# INDX purge-station setup guide

Calibrate `INDX_TC_VARS` in [`indx-tc-purge.cfg`](indx-tc-purge.cfg) so station enter, flush, brush, and exit stay inside soft limits and clear the wiper.

Live changes (lost on `RESTART` unless you edit the cfg):

```gcode
SET_GCODE_VARIABLE MACRO=INDX_TC_VARS VARIABLE=station_x VALUE=192.0
```

After a good set of numbers, copy them into `variable_*` in the cfg and `RESTART`.

---

## 0. Prerequisites

1. `[include indx-tc-purge.cfg]` is after your dock TC macros (`indx-tc-macros.cfg`).
2. Homing and `CHANGE_TOOL` / `T0` already work.
3. Know your soft limits (from `printer.cfg` / `position_min` / `position_max`, or console after home):
   - `GET_POSITION` (or Mainsail/Fluidd axis readouts)
   - Mentally: `X_min` … `X_max`, `Y_min` … `Y_max`, `Z_min` … `Z_max`
4. Keep a finger on emergency stop. First passes: cold nozzle, low travel speeds.

Useful console helpers:

```gcode
G28
GET_POSITION
INDX_TC_RETRACT STATE=query
```

`clear_y` used below is `TOOL_POSITIONS.clearance_y` if that macro exists, else `INDX_TC_VARS.clearance_y`.  
`dock_dir` is `TOOL_POSITIONS.dock_dir` if present, else `-1`.

---

## 1. Choose wipe axis

| Your wiper | Setting |
|------------|---------|
| Brush bristles / wipe stroke along **Y** (Prusa-like) | `wipe_along_x: 0` |
| Wipe stroke along **X** | `wipe_along_x: 1` |

```gcode
SET_GCODE_VARIABLE MACRO=INDX_TC_VARS VARIABLE=wipe_along_x VALUE=1
```

Flush wiggle, brush stroke, and eject jiggle follow this. Enter/exit still
use `clear_y` (dock clearance), not the wipe axis.

Also set `brush_x` / `brush_y` / spans in **machine coordinates** (true X and Y), not “rotated local” axes.

---

## 2. Safe Z first (`station_z` is a minimum)

1. Home, pick a tool (`CHANGE_TOOL TOOL=0`).
2. Jog XY over the purge/wipe area with Z high enough to clear everything.
3. Lower Z until the nozzle is at a comfortable purge height above the bin / brush (not touching).
4. Note that Z → `station_z`.

Behaviour: macros **only raise** Z to `station_z` when current Z is below it.
If the print is already at Z=45, station entry does **not** move Z down to 5.
Exit / `LAYER_Z` uses the same raise-only rule (never dive).

Checks:

- `station_z` must clear the brush tips and any bin lip when you *are* low.
- Optional `INDX_TC_POST ... LAYER_Z=` sets a raise-only resume floor (`+ resume_z_extra`).

Optional: temporarily lower `enter_feed` / `exit_feed` / `travel_speed` (e.g. 30–50 mm/s) while probing.

---

## 3. Set station centre (`station_x`, `station_y`)

This is where flush happens (bin / purge pot centre).

1. Jog to the centre of the purge area at `station_z`.
2. `GET_POSITION` → copy X into `station_x`, Y into `station_y`.
3. Confirm `clear_y` is on the **safe** side of the docks (same Y you use to travel past docks without clipping tools). If `TOOL_POSITIONS` is wrong, fix dock config; only then set `clearance_y` as a fallback.

Enter path (order matters):

1. Raise Z to at least `station_z` if needed  
2. `Y → clear_y` (at current X)  
3. `X → station_x` (at `clear_y`)  
4. `Y → station_y`  
5. `Z → station_z`

**Limit check for enter/exit alone:**

```text
X used: station_x   (plus whatever X you start from on the way to clear_y)
Y used: clear_y and station_y
```

Both must satisfy `Y_min ≤ y ≤ Y_max` and `X_min ≤ station_x ≤ X_max`.  
If the head starts at a dock X, the move `Y→clear_y` still uses that dock X - that X must be legal at `clear_y`.

---

## 4. Eject jiggle (along wipe stroke axis)

Runs immediately after arriving at the station, **before** the eject-temp
heat wait, so residue from the previous change is shaken off while it is
still solid enough to drop instead of smearing onto a warming nozzle.

| `wipe_along_x` | Positions |
|----------------|-----------|
| `0` | `Y = station_y` and `Y = station_y + dock_dir * eject_jiggle_dist` |
| `1` | `X = station_x` and `X = station_x + eject_jiggle_dir * eject_jiggle_dist` |

`eject_jiggle_dir` is `+1` or `-1` (default `-1`). Use it to shake toward the bin when wiping along X.

1. With nozzle at station, jog along the stroke axis a few mm toward the drop.
2. Set `eject_jiggle_dist` (start small, e.g. 3–5 mm) and `eject_jiggle_dir` if on X.
3. Limit check:

```text
# wipe_along_x=0
y_a = station_y
y_b = station_y + dock_dir * eject_jiggle_dist
Y_min ≤ min(y_a, y_b)  and  max(y_a, y_b) ≤ Y_max

# wipe_along_x=1
x_a = station_x
x_b = station_x + eject_jiggle_dir * eject_jiggle_dist
X_min ≤ min(x_a, x_b)  and  max(x_a, x_b) ≤ X_max
```

The other axis stays at the station coordinate during eject.

---

## 5. Flush wiggle (`flush_d_neg`, `flush_d_pos`)

Small moves along the **stroke** axis while extruding. Defaults ~1.3 / 1.5 mm are fine to start.

| `wipe_along_x` | Positions visited |
|----------------|-------------------|
| `0` | `station_y - flush_d_neg` … `station_y + flush_d_pos` (X fixed) |
| `1` | `station_x - flush_d_neg` … `station_x + flush_d_pos` (Y fixed) |

Limit check: those endpoints must stay inside soft limits and inside the bin (not into the brush frame yet - brush is a later move).

---

## 6. Brush box (`brush_*`)

1. Jog the nozzle to the **centre** of the usable brush face → `brush_x`, `brush_y`.
2. Measure how far you can stroke along the wipe direction without leaving the brush or hitting hardware → that half-length is the stroke span:
   - Stroke along Y (`wipe_along_x=0`): set `brush_y_span` so full stroke is `2 * brush_y_span`
   - Stroke along X (`wipe_along_x=1`): set `brush_x_span` so full stroke is `2 * brush_x_span`
3. Measure how wide the brush is **perpendicular** to the stroke → that is the pass fan-out span (`brush_x_span` if wiping in Y, `brush_y_span` if wiping in X).
4. `brush_passes` (default 3): more passes spread across the pass span.

**Envelope formulas**

Let:

```text
half_pass = (brush_passes - 1) / (2 * brush_passes) * pass_span
```

(`half_pass = 0` when `brush_passes == 1`)

| `wipe_along_x` | Stroke | Pass span | X range | Y range |
|----------------|--------|-----------|---------|---------|
| `0` | Y | `brush_x_span` | `brush_x ± half_pass` | `brush_y ± brush_y_span` |
| `1` | X | `brush_y_span` | `brush_x ± brush_x_span` | `brush_y ± half_pass` |

Require:

```text
X_min ≤ X_range_min  and  X_range_max ≤ X_max
Y_min ≤ Y_range_min  and  Y_range_max ≤ Y_max
```

Also leave a few mm margin inside soft limits; do not rely on endstop crash.

**Travel from station to brush:** the brush macro goes straight to the first brush corner/edge. Mentally check that the straight segment from `(station_x, station_y)` to the brush approach point does not clip the docks or bin walls. If it does, move `brush_*` or `station_*`, or raise Z (`station_z` is used for station work; brush currently stays at the Z left by the station - keep `station_z` high enough for the whole brush path).

---

## 7. Full post-TC XY box (before first hot run)

Compute the union (all numbers in mm):

```text
# Always
X candidates: station_x, brush X range from §6,
              (if wipe_along_x=1: station_x + eject_jiggle_dir * eject_jiggle_dist),
              (if wipe_along_x=1: flush X endpoints from §5)
Y candidates: clear_y, station_y,
              (if wipe_along_x=0: station_y + dock_dir * eject_jiggle_dist),
              (if wipe_along_x=0: flush Y endpoints from §5),
              brush Y range from §6
```

```text
X_min_post = min(all X candidates)
X_max_post = max(all X candidates)
Y_min_post = min(all Y candidates)
Y_max_post = max(all Y candidates)
```

Pass only if that box sits strictly inside soft limits (recommend ≥2–5 mm margin).

---

## 8. Speeds and heat (leave Prusa-ish until motion is proven)

| Variable | Role | First-test tip |
|----------|------|----------------|
| `enter_feed` / `exit_feed` / `travel_speed` | XY travel | 30–60 mm/s until happy |
| `brush_feed` | Wipe speed | Slow enough not to deflect the brush mount |
| `eject_jiggle_feed` | Blob shake | Moderate |
| `purge_volume_mm3` | Flush volume | Keep 12 until flow looks right |
| `eject_temp_*` / `near_full_delta` | Staged heat | Defaults OK |

Retract / latch (do not treat as XY):

- Tip retract must stay **well under** latch unlock (~11 mm).
- With `unretract_after_exit=0`, the slicer unretracts after it travels to the next print XY. Tip retract must be **<=** that unretract (usually Orca `retraction_length` / `new_retract_length`). If tip is longer, the first extrusion stays short. Do not size tip to `retract_restart_extra_toolchange` - Orca often ignores that with the prime tower off.
- Per-filament: pass `RETRACT={new_retract_length}` into `INDX_TC_POST`. Tip becomes `max(0, RETRACT - post_purge_adjust)`. Set `post_purge_adjust` on the printer (e.g. `0.2`) for a little net prime after unretract. Fallback without `RETRACT=` is `post_purge_retract`. Absolute override: `POST_PURGE_RETRACT=`.
- `retract_toolchange` (8) is filament pull for deretract, not latch unlock.

---

## 9. Staged motion tests (cold → warm → full)

Do these in order. Abort on any unexpected move.

### A. Geometry only (no purge)

```gcode
G28
CHANGE_TOOL TOOL=0
# Optional: set station_* / brush_* live, then:
```

Jog-verify each pose you programmed (`station`, brush corners) with `G0`/`G1` at low F, or temporarily call helpers if you are comfortable:

- Mentally walk `_INDX_TC_GOTO_STATION` → eject (before heat) → flush → brush → exit Y.

There is no built-in dry-run dump of expanded G-code; use jog + the envelope maths in §7.

### B. Heat wait only

```gcode
CHANGE_TOOL TOOL=0
INDX_TC_POST TEMP=220 SKIP_PURGE
```

Confirms staging wait without station motion.

### C. First full station cycle (expect purge / ooze)

Nozzle at print temp, bin empty enough to catch filament:

```gcode
CHANGE_TOOL TOOL=0
INDX_TC_POST TEMP=220
```

Watch: enter via `clear_y`, flush on stroke axis, brush stroke direction, exit to `clear_y`, tip unretract if `unretract_after_exit=1`.

### D. Multi-tool line test

```gcode
INDX_TC_PURGE_TEST TEMP=220
```

Optional: `BED_TEMP=60`, `TOOLS=2` (only T0..T2), `LINE_LEN=120`, `LINE_GAP=15`, `SKIP_PURGE` for dock-only.

Also check the **bed line** box from the file header (centred on axis mid) does not hit clips or the wipe assembly.

### E. Slicer

Toolchange G-code:

```gcode
T{next_extruder} TEMP={temperature[next_extruder]}
M400
INDX_TC_POST TEMP={temperature[next_extruder]} TYPE={filament_type[next_extruder]} RETRACT={new_retract_length}
```

`TYPE=` selects material-specific purge speed (`TPU` uses a slower fast purge, ~8 mm3/s). Pass the same on start via `PRINT_START ... TYPE={filament_type[initial_tool]}`.

`RETRACT={new_retract_length}` is the incoming filament's retraction length (what Orca typically unretracts after the TC travel). Tip retract = that value minus `post_purge_adjust`. After slicing, confirm the unretract `G1 E...` matches and tip <= that length.

No second full heat-wait in the slicer. Slice a two-colour part and confirm first extrusion after each TC.

---

## 10. Persist and re-check after restart

1. Write final numbers into `variable_*` in `indx-tc-purge.cfg`.
2. `RESTART` (or firmware restart).
3. Re-run §7 arithmetic once (defaults must match what you jogged).
4. Re-run §9 C or D.

---

## Quick checklist

- [ ] `wipe_along_x` matches physical wiper
- [ ] `station_x/y/z` = purge centre / height
- [ ] `clear_y` / `TOOL_POSITIONS.clearance_y` safe past docks
- [ ] Eject endpoints along stroke axis inside limits and over the bin
- [ ] Flush endpoints inside bin
- [ ] Brush X/Y ranges inside limits and on bristles
- [ ] Full post-TC box inside soft limits with margin
- [ ] Tip retract ≪ latch unlock, and tip <= slicer post-TC unretract (`RETRACT={new_retract_length}`)
- [ ] `SKIP_PURGE` OK → full `INDX_TC_POST` OK → `INDX_TC_PURGE_TEST` OK

XY envelope reference (same formulae) also lives in the header comment block of `indx-tc-purge.cfg`.
