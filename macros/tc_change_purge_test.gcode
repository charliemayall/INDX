; tc_change_purge_test.gcode
; Bench script: dock swap (T0 -> T1) then INDX_TC_POST purge station.
;
; Matches the Orca change_filament_gcode contract in indx-tc-purge.cfg:
;   T{next} TEMP={temp}
;   M400
;   INDX_TC_POST TEMP={temp} TYPE={type} RETRACT={retract}
;
; Prerequisites:
;   - indx-tc-macros.cfg and indx-tc-purge.cfg included in printer.cfg
;   - T0 seated on the head before running (or uncomment the T0 pick below)
;   - Purge station geometry calibrated (see INDX_TC_PURGE_SETUP.md)
;
; Edit TEMP / TYPE / RETRACT / tool indices below as needed.

M83
G90
G21
M220 S100
M221 S100

; Optional: phase timing logs (grep klippy.log for TC_TIMING:)
TC_TIMING ENABLE=1

INDX_TC_RESET

; Home XY; fake-home Z so CHANGE_TOOL can Z-hop.
G28 Y
G28 X
SET_KINEMATIC_POSITION Z=0 SET_HOMED=Z

; Uncomment if the head is empty (no tool loaded):
; T0 TEMP=210
; M400

; --- toolchange T0 -> T1, then purge ---
T1 TEMP=210
M400
INDX_TC_POST TEMP=210 TYPE=PLA RETRACT=0.8

M117 TC change + purge done
