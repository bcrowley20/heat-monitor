// Copy this file to wifi_config.h in the same folder and fill in your network.
// wifi_config.h is kept out of git.
#define WIFI_SSID "your network name"
#define WIFI_PASSWORD "your network password"
// Needed to send firmware updates over Wi-Fi. Pick your own; platformio.ini reads the
// same value from the PUMP_OTA_PASSWORD environment variable when uploading.
#define OTA_PASSWORD "choose an update password"
