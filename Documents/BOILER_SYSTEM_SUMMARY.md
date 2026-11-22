# Radiant Heating System: Background, Changes and Current State

Context for planning the monitoring and instrumentation work. This file covers what the owner set out to understand, what was changed between 2026-10-24 and 2026-11-01, and how the system stands now.

**Status as of 2026-11-16.** It reflects everything known through the last update on 2026-11-01. Items marked **[unconfirmed]** have not been verified since. See also `HEATING_MONITOR_PLAN.md`, the earlier data-collection app plan (Nest, Weather Underground, CentriConnect).

---

## 1. The house and system

### House
- About 1,850 sq ft, single level, near Cle Elum, WA, in central Washington. Winter lows reach −15°F.
- Walls are 2x6, half spray foam and half fiberglass. Ceilings are 10 ft. The Great Room is vaulted from 9 ft to 23 ft.
- The Great Room has large north-facing glass, including an 11' × 8.8' four-panel folding door, plus five windows.
- A propane fireplace in the Great Room provides backup heat.

### Heating emitters
- PEX tubing is embedded in the concrete slab, with 2" rigid foam (R-Tech) underneath. Every zone is in the slab; there are no baseboards or panel radiators.
- Vertical slab-edge insulation could not be seen in the construction photos **[unconfirmed]**.
- Tube spacing is about 12" in most rooms and about 6" in the WC and Powder room, estimated from the 6" mesh in the photos.

### Boiler
- **Lochinvar Knight WHN110,** a wall-hung, condensing, fire-tube boiler running on propane, with an older Smart System control.
- **Ratings:** 110,000 BTU/hr input, about 102,000 gross output, 89,000 net (AHRI). The net rating is gross ÷ 1.15, a sizing allowance, not lost heat.
- **Modulation:** 20–100%, so minimum fire is about 22,000 BTU/hr input, or about 21,000 BTU/hr into the water. This is the boiler's real turndown, not the 10:1 of newer Knights.
- **PC port:** a phone-style jack that needs Lochinvar's proprietary SMART SYSTEM PC kit. A generic serial adapter will not work. It is not a practical data source.
- **Venting:** direct vent, 3" pipe. According to the technician, the intake and exhaust runs are about twice Lochinvar's maximum length (see section 6).

### Distribution
- **Manifolds:** two Uponor manifolds in the Utility closet on the south wall. The supply manifold has the flow meters; the return manifold has the actuators.
- **Actuators:** Uponor TSA 01 TYP2 thermal actuators with no end switches. Brown wire to Taco output terminal 1, blue to terminal 2, and terminals 3–4 jumpered.
- **Zone control:** Taco ZVC406 zone valve controller. The model suffix may be -5 or -6 **[unconfirmed]**. Each zone output is rated 24 VA, judging from the ZVC404 sheet of the same family.
- **Pumps:** a system pump moving about 3 GPM with two zones open, a DHW pump, and the boiler's internal pump.
- **Nine loops** on ports 1–19; port 9 is a capped spare.

### Thermostats
- **Four Google Nest thermostats.** A fifth, on the bath, was removed on 2026-10-27.
- **Wiring:** 18/5 cable at each Nest (red R, white W, blue C; green and yellow unused). In the utility closet junction box, WAGO splices join it to 18/3 cable running to the Taco: red to red, white to white, blue to green.

### Domestic hot water
- **Tank:** Squire SIT065, a 65-gallon stainless indirect tank heated by the boiler through the DHW pump.
- **Control:** the tank has no aquastat. A tank sensor is wired to the Knight, so the boiler controls tank temperature.
- **DHW night setback** on the Knight: tank target dropped to 90°F from 10 PM to 5 AM.
- **Recirculation pump:** present, on a timer **[schedule unconfirmed]**.

### Other data sources
- **Outdoor temperature:** the owner's own Weather Underground station KWACLEEL38, about 100 ft from the house.
- **Propane tank:** CentriConnect monitor (Home Assistant integration `aiocentriconnect`).
- **Pump monitoring:** the owner now monitors the system pump and DHW pump, which separates space-heating calls from DHW calls.

---

## 2. Zones and loops (current, since 2026-10-27)

Room names are the owner's, with the floor-plan (sheet A2.1) name in parentheses where they differ. "Ports" are manifold supply/return ports; the actuator sits on the return port.

| Taco zone | Thermostat | Return port: loop label (area) | Tubing | Est. length |
|---|---|---|---|---|
| 1 | Guest Bedroom Nest (M.I.L.) | 3: M. Bed (Guest Bedroom); 1: Laundry | gray | ~200 ft (owner's figure 150); ~90 ft |
| 2 | W jumpered from Zone 1 | 7: Bath (Master Bath) | gray | 144 ft (owner's figure) |
| 3 | Hallway Nest | 10: Hall W. (Entry, Vestibule, west hall); 5: Hall Bath (WC, Powder, east hall and Back Entry, coverage estimated) | gray; red in Powder | ~135 ft; ~180 ft |
| 4 | Master Bedroom Nest (Guest on plan) | 12: Bed | gray | ~240 ft |
| 5 | Great Room Nest | 14: Living N. (north strip along the glass); 16: Living M. | clear; red | ~270 ft; ~240 ft |
| 6 | W jumpered from Zone 5 | 18: Living S. | red | ~225 ft |

- **Zone jumpers:** only W is jumpered between Zones 1 and 2 and between Zones 5 and 6; R and C on Zones 2 and 6 are unused. The owner tested all zones on 2026-10-27, and the correct actuators opened.
- **Heating zones:** effectively four: North (Zones 1 + 2), Hallway (3), Master Bedroom (4) and Great Room (5 + 6).
- **Lengths:** except where they are the owner's figures, the lengths are estimates (floor area ÷ spacing + manifold runs), good to about ±20%. They total roughly 1,700 ft.
- **Leader runs:** the supply and return lines from the manifold to the Great Room, Master Bedroom and Hall W. loops run west under the Vestibule and Entry. They warm the hallway even when its zone is not calling.
- **Master Bedroom:** the owner keeps it turned down for cooler sleeping.

---

## 3. What the owner set out to understand

1. **Setback strategy.** The original schedule was 68°F by day and 66°F from 8 PM to 5 AM. The question was whether a better strategy existed for a high-mass slab.
2. **Boiler efficiency and setup.** Whether outdoor reset, boost and similar settings were configured sensibly for a slab-only system with no mixing valve.
3. **Short cycling.** The boiler was running about 4-minute burns: it ramped to 100% within 90 seconds, overshot the target and shut off on Setpoint Met.
4. **Low-fire noise.** A moaning or humming sound whenever the boiler fires at about 20%, its minimum rate. It is quiet at 30% and above.
5. **Sizing.** Whether the boiler is oversized for the house, and whether the Great Room floor can keep up at −15°F.
6. **Zoning.** Small single-loop zones are below the boiler's minimum output. The hallway runs warm, and the laundry loop was starved while it was on the hallway thermostat.
7. **DHW propane.** About 0.75 gal/day for hot water, and how to reduce it.
8. **Measuring results.** Weather-normalized propane per heating degree day and boiler runtime, before and after each change.

---

## 4. Timeline of changes

| Date | Change |
|---|---|
| 2026-10-25 ~20:30 | **Ramp delay enabled.** Steps 15/20/25/30/40% at 5 minutes each, step 6 ceiling 60%. Previously off. |
| 2026-10-25 ~21:35 | **Outdoor reset curve entered** (section 5). **SH1 Offset** 5 → 10°F. **Anti-Cycling** 1 → 10 min. The earlier curve gave about 106–107°F at 40°F outside and was not recorded. |
| 2026-10-25 ~21:55 | Ramp steps 1–2 raised to 25% to avoid the noise (did not help). **SH1 Set Point at High Outdoor Temp** raised 80 → 90°F; this was later confirmed as the current value. |
| 2026-10-25 night | Thermostats turned down to 64–65°F overnight to avoid the noise. The house held 67–68°F with no burner run. |
| 2026-10-26 | **Technician visit.** The moaning was attributed to vent runs about twice the maximum length, or a dirty heat exchanger. Ramp steps 1–2 returned to 15/20%. |
| 2026-10-26 | **Intake reroute planned:** move the air intake from the roof to the sidewall ("Vertical Vent, Sidewall Air", manual Figure 3-5). **[completion unconfirmed]** |
| 2026-10-27 | **Zones 4 → North group.** The Bath thermostat was removed. Zone 1 W was jumpered to Zone 2 W. The Laundry actuator moved from the Hallway zone to Zone 1 (with M.I.L.). Tested OK. |
| 2026-10-28 | Wiring diagram documented: thermostat cable splices, zone map and actuators. |
| 2026-11-01 | **Recirculation pump unplugged for one day** as a test of DHW cycling. **[result and pump's current state unconfirmed]** |

Thermostat practice since 2026-10-25: hold steady setpoints of about 67–68°F, with no nightly setback. The Master Bedroom is kept cooler.

---

## 5. Current boiler settings

| Parameter | Value | Note |
|---|---|---|
| Low Outdoor Temperature 1 | 5°F | |
| SH1 Set Point at Low Outdoor Temp | 110°F | |
| High Outdoor Temperature 1 | 65°F | |
| SH1 Set Point at High Outdoor Temp | 90°F | Confirmed 2026-10-27. The target was 101°F at 31°F outdoor. |
| Outdoor Air Shutdown SH1 | 65°F | |
| SH1 Set Point / Min / Max | 120 / 80 / 120°F | The 120°F max is the slab's only protection; there is no mixing valve. |
| SH1 Offset | 10°F | The burner stops at target + 10°F. |
| SH1 Differential | 20°F | It relights at target + 10 − 20 = target − 10°F. |
| Anti-Cycling Time | 10 min | |
| Ramp delay | On: 15, 20, 25, 30, 40% at 5 min each; step 6 = 60% (about 60,000 BTU/hr ceiling) | Restarts at step 1 on every space-heating call **and** after every DHW → space-heating switch (manual p. 66). |
| Boost / SH Night Setback | recommended off | **[unconfirmed]** |
| DHW night setback | Tank target 90°F, 10 PM–5 AM | |
| DHW tank set point, DHW differential, DHW boiler (supply) set point | **[unknown]** | Recommended: about 120°F tank, a wider differential, and about 150–160°F supply. |

**Curve check:** target = 90 + (65 − T_out) × (110 − 90) ÷ (65 − 5). That gives about 93°F at 55°F outside, 101°F at 31°F, and holds 110°F at or below 5°F.

**For deep cold:** if the Great Room falls behind below 5°F, the plan is to set Low Outdoor Temperature to −15°F with 115–120°F at that end, and/or raise ramp step 6 to 70–80%.

---

## 6. Key observations and measurements

### Hydronics (2026-10-25, about 40–41°F outside)
- **Loop flows:** Great Room loops 0.7 GPM each; Bath 0.85 GPM; system total about 3 GPM with two zones calling.
- **Before the changes:** about 4-minute burns, ramping to 100% within 90 s and overshooting a 107°F target to 110°F.
- **After ramp delay and offset:**
  - One burn ran about an hour, from about 20:26 to 21:25.
  - The firing rate hunted between 20% and 60%. Steps below the target led to ramp-ups, and passing the target led to cut-backs.
- **Two zones, steady at 20% fire:** supply 93°F, return 80°F (drop 13°F), about 19,500 BTU/hr.
- **Great Room alone at about 58% fire:** supply 110°F, return 90°F (drop 20°F), about 21,000 BTU/hr.
- **A 20°C (36°F) drop read right after ignition** was a start-up transient; the steady-state drop is 12–13°F.
- **Slab surface (thermal camera):** Bath area 69–71°F; Great Room 64–68°F while its zone was still recovering.

### Hydronics (later)
- **2026-10-27, 31°F outside:**
  - The Great Room called from 01:30 and the boiler held 25% for more than 4 hours. Supply 100°F, return 90°F, target 101°F. The room went from 66°F to 67°F by 05:50.
  - When Bath and Guest Bedroom also called, the boiler went to 60%, overshot, and modulated down instead of shutting off.
- **2026-10-27 midday:** the system was off from 08:00 to 12:00 and rooms read 68–69°F. That came from slab coasting plus sun, with about 1–2°F of overshoot after a long burn. Nest "True Radiant" was suggested **[status unconfirmed]**.
- **2026-10-28, 35°F outside:** the boiler did not run overnight. Rooms held 67–69°F.
- **Hallway:** it rarely calls, because it is warmed by the leader runs and the boiler closet.

### Low-fire noise
- It happens at about 20% fire, the boiler's minimum, and is quiet at 30% and above.
- It does not depend on water temperature; raising the target to about 98°F did not help.
- The control has **flame current support,** which raises the firing rate if the flame signal is too low. A weak flame at 20% is a plausible clue.
- **Venting rules from the WH-I-O Rev Y manual:**
  - "Vertical Vent, Sidewall Air" is allowed.
  - Minimum length is 12 equivalent ft, counting 5 ft per 90° elbow and 3 ft per 45°.
  - Maximum for the WHN110 on 3" pipe is 100 ft. Whether that applies to each pipe or combined is ambiguous.
  - Changing pipe size is not authorized.
  - The intake ends in a down-turned elbow at least 12" above grade or snow line.
  - Cellular-core pipe is prohibited, at least for the exhaust.
- **Planned intake:** about 3 ft of 3" Schedule 40 solid-wall PVC, plus an inside sweep and an outside down-turned elbow, about 13 equivalent ft in total.
- **Exhaust:** its length still needs checking against the limit.
- **Combustion analysis:** CO₂ and CO readings at minimum and maximum fire are still outstanding.

### Loads and sizing (rough estimates, not measured)
- **Whole house at −15°F:** about 45,000–65,000 BTU/hr. The boiler is roughly 1.5–2.5× oversized at peak, and more on typical days.
- **Great Room at −15°F:** about 16,000–23,000 BTU/hr. Its floor can deliver only about 15,000–17,500 BTU/hr at an 85°F surface, so the floor may fall slightly short on the coldest days; the fireplace covers the gap.
- **Combined North zone:** roughly 11,000–23,000 BTU/hr, close to the boiler's minimum output.
- **Hallway zone alone:** still below the minimum output.

### DHW (2026-10-31, full day)
- **10 DHW pump runs, 75.6 min total.** Hot water calls skip the ramp delay and fire hard.
- **No calls from 00:31 to 07:59,** even though the setback ended at 05:00. The tank's own standby loss is small.
- **The afternoon calls were evenly spaced** at 11:57, 14:14, 16:27 and 18:40, about every 2 h 13 min, each 7–8 min. That pattern suggests the recirculation pump on a daytime timer, not hot water use. It accounts for an estimated 30 min of firing, about 40% of DHW propane.
- **The 00:27 call** fell during the setback and was probably a hot water draw (cold water reaching the sensor).
- **Each DHW call interrupts space heating** and restarts the ramp delay.

---

## 7. Open questions the instrumentation should answer

1. **Recirculation pump impact.** DHW firing minutes and calls per day with the pump on, off, or on a tighter timer or on-demand control. Separate standby and recirc loss from real use.
2. **Burner runtime vs. zone-call time.** Nest "HEATING" and the Taco zone outputs show demand, not firing. Burner time differs during anti-cycling, setpoint-met periods and DHW priority.
3. **Burn length and cycle count** per day vs. outdoor temperature, before and after each change. Short cycles: how many, and when (for example, a single small zone calling alone).
4. **Time at minimum fire.** How often the boiler sits at 20%, where the noise occurs. Whether the intake reroute or a heat exchanger cleaning removes the noise.
5. **Propane per heating degree day** before and after 2026-10-25, using the delivery invoices as the pre-change baseline.
6. **Condensing performance.** Return water temperature over time. It has been 78–90°F in heating mode; DHW reheats likely run hotter.
7. **Great Room capacity in deep cold.** Whether the room holds its setpoint below 5°F, and the supply temperature it needs.
8. **Overshoot after long burns** (about 1–2°F), and whether Nest True Radiant or a lower curve reduces it.

### Signals worth capturing (ideas for the plan)
- **Boiler firing:** burner on/off, ideally the firing rate. Options include a current clamp on the boiler circuit, where fan and igniter draw indicate firing, or the 24 V gas valve signal, if it can be read safely. The PC port is not practical.
- **Temperatures:** boiler supply and return (or the manifold supply and return), DHW tank, and flue gas (an optional check on condensing). Clamp-on sensors on the pipes are enough.
- **Zone calls:** the six Taco zone outputs (24 VAC each), for exact zone-call timing that does not depend on Nest polling.
- **Pumps:** the system pump and DHW pump (already instrumented) and the recirculation pump.
- **DHW draw:** a flow meter on the tank's cold inlet would separate usage from losses.
- **Context:** outdoor temperature from KWACLEEL38, propane level from CentriConnect, and Nest setpoints and ambient temperatures.

---

## 8. Things to confirm with the owner

- Was the intake moved to the sidewall? Has the heat exchanger been cleaned? Has combustion been analysed? Is the noise still present?
- What was the result of the 2026-11-01 recirculation pump test, and what is the pump's current schedule?
- What are the current DHW tank set point, DHW differential and DHW boiler set point?
- Are Boost and SH Night Setback off, and is the Taco priority switch off?
- Is the Taco model suffix -5 or -6?
- Is Nest True Radiant (heating type: radiant) enabled on each thermostat?
- How long is the exhaust vent, in equivalent feet?
- What size is the propane tank?
