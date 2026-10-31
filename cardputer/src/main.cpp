// Boiler pump sensor for the original M5Stack Cardputer (StampS3).
//
// Watches two inputs on the Grove port, one per pump, and serves their state as JSON:
//
//     GET /                          current state and the last 20 switches
//     GET /events?after=N&boot=ID    switches numbered above N (what heatmon uses)
//
// Each pump's 5 V USB adapter feeds its pin through a 10k/15k divider, so a running pump
// reads high. A change counts only after it has held for SETTLE_US, and it is timed from
// when it was first seen. Times are seconds since boot, so no clock needs setting.
// The screen shows each pump, the network address and when heatmon last asked.

#include <M5Cardputer.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_task_wdt.h>

#include "wifi_config.h"  // WIFI_SSID and WIFI_PASSWORD; copy wifi_config.example.h

struct Pump {
  const char *name;   // the name heatmon knows the pump by
  const char *label;  // shown on the screen
  int pin;
  bool on;
  int64_t since;  // microseconds since boot
  bool pending;
  int64_t pending_since;
};

// Grove port signal pins. Swap the numbers if the screen shows the pumps the wrong way round.
Pump pumps[] = {{"system", "System", 1, false, 0, false, 0}, {"dhw", "DHW", 2, false, 0, false, 0}};
const int N_PUMPS = sizeof(pumps) / sizeof(pumps[0]);

const int PORT = 8081;
const int SAMPLE_MS = 20;
const int64_t SETTLE_US = 200000;
const uint32_t MAX_EVENTS = 2000;  // switches kept; heatmon notices if it falls further behind
const uint32_t PAGE = 200;         // switches per /events reply
const uint32_t RECENT = 20;        // switches shown at /
const int64_t WIFI_RESTART_US = 120000000;  // start Wi-Fi over after this long offline
const int WATCHDOG_S = 15;         // restart if the program stops running this long

struct Event {
  int64_t at;
  uint8_t pump;
  bool on, start;
};
Event events[MAX_EVENTS];  // switch number s is kept at (s - 1) % MAX_EVENTS
uint32_t seq = 0;          // number of the newest switch
SemaphoreHandle_t lock;    // guards pumps, events and seq between the two tasks

char boot_id[17];
WebServer server(PORT);
M5Canvas canvas(&M5Cardputer.Display);
int64_t last_asked = -1;  // when heatmon last asked for switches
volatile uint8_t wifi_reason = 0;  // why the last Wi-Fi attempt failed, 0 if none yet

const char *wifi_problem(uint8_t reason) {
  switch (reason) {
    case WIFI_REASON_NO_AP_FOUND: return "network not found (2.4 GHz on?)";
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "wrong password?";
    case WIFI_REASON_AUTH_EXPIRE:
    case WIFI_REASON_ASSOC_FAIL: return "router refused to connect";
    default: return nullptr;
  }
}

int64_t now_us() { return esp_timer_get_time(); }

uint32_t first_seq() { return seq == 0 ? 0 : (seq > MAX_EVENTS ? seq - MAX_EVENTS + 1 : 1); }

void add_event(int pump, bool on, int64_t at, bool start) {
  seq++;
  events[(seq - 1) % MAX_EVENTS] = {at, (uint8_t)pump, on, start};
}

bool read_pin(const Pump &p) { return digitalRead(p.pin) == HIGH; }

void sample() {
  int64_t now = now_us();
  xSemaphoreTake(lock, portMAX_DELAY);
  for (int i = 0; i < N_PUMPS; i++) {
    Pump &p = pumps[i];
    bool v = read_pin(p);
    if (v == p.on) {
      p.pending = false;
    } else if (!p.pending) {
      p.pending = true;
      p.pending_since = now;
    } else if (now - p.pending_since >= SETTLE_US) {
      p.on = v;
      p.since = p.pending_since;
      p.pending = false;
      add_event(i, v, p.since, false);
    }
  }
  xSemaphoreGive(lock);
}

// Runs apart from the web server, so a slow request never delays reading the pins.
void sampler(void *) {
  esp_task_wdt_add(nullptr);
  TickType_t wake = xTaskGetTickCount();
  for (;;) {
    sample();
    esp_task_wdt_reset();
    vTaskDelayUntil(&wake, pdMS_TO_TICKS(SAMPLE_MS));
  }
}

void append(String &out, const char *fmt, ...) {
  char buf[160];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof buf, fmt, args);
  va_end(args);
  out += buf;
}

// The same JSON as the old Raspberry Pi program, minus its wall clock times.
// With `paged`, switches numbered above `after`; if `boot` isn't this run's ID, the
// caller has missed a restart and gets all of them.
String snapshot(bool paged, uint32_t after, const String &boot) {
  String out;
  out.reserve(PAGE * 80 + 512);
  xSemaphoreTake(lock, portMAX_DELAY);
  int64_t now = now_us();
  uint32_t first = first_seq(), from, to = seq;
  bool more = false;
  if (!paged) {
    from = seq > RECENT ? seq - RECENT + 1 : 1;
    if (from < first) from = first;
  } else {
    if (boot != boot_id) after = 0;
    // Start at the oldest switch still kept before sizing the page, so a caller that
    // fell behind gets those switches instead of an empty page.
    from = after >= seq ? seq + 1 : after + 1;
    if (from < first) from = first;
    if (to >= from && to - from + 1 > PAGE) {
      to = from + PAGE - 1;
      more = true;
    }
  }
  append(out, "{\"boot_id\":\"%s\",\"mono_now\":%.3f,\"pumps\":{", boot_id, now / 1e6);
  for (int i = 0; i < N_PUMPS; i++) {
    const Pump &p = pumps[i];
    append(out, "%s\"%s\":{\"on\":%s,\"since_mono\":%.3f,\"for_s\":%.1f}", i ? "," : "", p.name,
           p.on ? "true" : "false", p.since / 1e6, (now - p.since) / 1e6);
  }
  append(out, "},\"first_seq\":%lu,\"last_seq\":%lu,\"more\":%s,\"events\":[", (unsigned long)first,
         (unsigned long)seq, more ? "true" : "false");
  for (uint32_t s = from; seq && s <= to; s++) {
    const Event &e = events[(s - 1) % MAX_EVENTS];
    append(out, "%s{\"seq\":%lu,\"pump\":\"%s\",\"on\":%s,\"mono\":%.3f,\"start\":%s}", s > from ? "," : "",
           (unsigned long)s, pumps[e.pump].name, e.on ? "true" : "false", e.at / 1e6, e.start ? "true" : "false");
  }
  out += "]}";
  xSemaphoreGive(lock);
  return out;
}

void handle_root() { server.send(200, "application/json", snapshot(false, 0, "")); }

void handle_events() {
  String after = server.arg("after");
  if (after.isEmpty()) after = "0";
  for (char ch : after) {
    if (!isdigit(ch)) return server.send(400, "text/plain", "after must be a whole number");
  }
  last_asked = now_us();
  server.send(200, "application/json", snapshot(true, strtoul(after.c_str(), nullptr, 10), server.arg("boot")));
}

String how_long(int64_t us) {
  int64_t s = us / 1000000;
  char buf[24];
  if (s < 60) snprintf(buf, sizeof buf, "%lld s", s);
  else if (s < 3600) snprintf(buf, sizeof buf, "%lld min", s / 60);
  else if (s < 86400) snprintf(buf, sizeof buf, "%lld h %02lld min", s / 3600, s / 60 % 60);
  else snprintf(buf, sizeof buf, "%lld d %lld h", s / 86400, s / 3600 % 24);
  return buf;
}

void draw() {
  Pump shown[N_PUMPS];
  xSemaphoreTake(lock, portMAX_DELAY);
  for (int i = 0; i < N_PUMPS; i++) shown[i] = pumps[i];
  xSemaphoreGive(lock);
  int64_t now = now_us();

  canvas.fillScreen(TFT_BLACK);
  canvas.setTextSize(1);
  canvas.setTextColor(TFT_LIGHTGREY);
  canvas.setCursor(4, 4);
  if (WiFi.status() == WL_CONNECTED) canvas.printf("%s:%d", WiFi.localIP().toString().c_str(), PORT);
  else canvas.printf("Wi-Fi: connecting to %s", WIFI_SSID);
  if (WiFi.status() != WL_CONNECTED && wifi_reason) {
    const char *why = wifi_problem(wifi_reason);
    canvas.setCursor(4, 13);
    canvas.setTextColor(TFT_ORANGE);
    if (why) canvas.printf("failed: %s (%d)", why, wifi_reason);
    else canvas.printf("failed: reason %d", wifi_reason);
  }

  for (int i = 0; i < N_PUMPS; i++) {
    int y = 24 + i * 44;
    canvas.setTextSize(3);
    canvas.setTextColor(TFT_WHITE);
    canvas.setCursor(4, y);
    canvas.print(shown[i].label);
    canvas.setTextColor(shown[i].on ? TFT_GREEN : TFT_DARKGREY);
    canvas.setCursor(140, y);
    canvas.print(shown[i].on ? "ON" : "off");
    canvas.setTextSize(1);
    canvas.setTextColor(TFT_LIGHTGREY);
    canvas.setCursor(4, y + 26);
    canvas.printf("for %s", how_long(now - shown[i].since).c_str());
  }

  canvas.setCursor(4, 122);
  if (last_asked < 0) canvas.print("heatmon has not asked yet");
  else canvas.printf("heatmon asked %s ago", how_long(now - last_asked).c_str());
  canvas.pushSprite(0, 0);
}

void setup() {
  auto cfg = M5.config();
  cfg.external_display_value = 0;  // don't probe the Grove port for add-on screens; it holds the pump pins
  M5Cardputer.begin(cfg, false);  // no keyboard; the pump pins need nothing else
  M5Cardputer.Display.setBrightness(60);
  canvas.createSprite(M5Cardputer.Display.width(), M5Cardputer.Display.height());

  // INPUT turns off the chip's own pull-up and pull-down. The divider's bottom resistor
  // holds the pin low when a pump is off.
  for (auto &p : pumps) pinMode(p.pin, INPUT);
  lock = xSemaphoreCreateMutex();
  int64_t now = now_us();
  for (int i = 0; i < N_PUMPS; i++) {
    pumps[i].on = read_pin(pumps[i]);
    pumps[i].since = now;
    add_event(i, pumps[i].on, now, true);
  }

  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
    wifi_reason = info.wifi_sta_disconnected.reason;
    Serial.printf("Wi-Fi failed, reason %d\n", wifi_reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  WiFi.setHostname("pump-sensor");
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // answer quickly instead of saving power
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  snprintf(boot_id, sizeof boot_id, "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());

  server.on("/", handle_root);
  server.on("/events", handle_events);
  server.onNotFound([] { server.send(404, "text/plain", "not found"); });
  server.begin();

  // If either the pin reader or the web server hangs, restart rather than serve a frozen state.
  esp_task_wdt_init(WATCHDOG_S, true);
  enableLoopWDT();
  xTaskCreatePinnedToCore(sampler, "sampler", 4096, nullptr, 2, nullptr, 1);
  Serial.printf("Pump sensor starting, boot %s\n", boot_id);
}

void loop() {
  static int64_t last_draw = 0, last_online = 0;
  server.handleClient();
  int64_t now = now_us();
  // Auto-reconnect handles most drops. This catches the rare case where it gives up,
  // and waits long enough never to cut off a join that is still going.
  if (WiFi.status() == WL_CONNECTED) {
    last_online = now;
  } else if (now - last_online > WIFI_RESTART_US) {
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    last_online = now;
  }
  if (now - last_draw > 500000) {
    draw();
    last_draw = now;
  }
  delay(2);
}
