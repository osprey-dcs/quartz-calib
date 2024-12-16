import cothread
from cothread.catools import *
from datetime import date, datetime
import math
import numpy
from socket import *
import sys
import time


#dmm_ip = '192.168.83.47'
#dmm_port = 5025
dmm_ip = '192.168.79.97'
dmm_port = 5025
#afg_ip = '192.168.83.48'
#afg_port = 5025
afg_ip = '192.168.79.98'
afg_port = 5025

num_adc_channels = 32

cmd_idn = b'*IDN?\n'
cmd_read = b'READ?\n'
cmd_appl_read = b'VOLT:RANGE:AUTO?\n'

# For reasons unexplained at this point, the function generator outputs a DC
# voltage twice of that which is commanded.
cmd_set_pos = b'APPL:DC DEF, DEF, 9.0\n'    # +9V
cmd_set_neg = b'APPL:DC DEF, DEF, -9.0\n'   # -9V
cmd_set_zero = b'APPL:DC DEF, DEF, 0\n'     # 0V

dmm_deadband = 0.01
calib_date = date.today()
calib_time = datetime.now().strftime('%H:%M:%S')
file_time = datetime.now().strftime('%H-%M-%S')


overall_pass = True         # Goes False if any channel fails calibration

expected_count = 5560000    # Approx count value for 9v input
count_tolerance = 10000     # number of counts difference we tolerate
offset_tolerance = .01      # largest offset we tolerate (we expect zero as offset)
expected_slope = 1.6e-6     # Expected slope values based on previous runs
slope_tolerance = 1e-6      # largest slope variation we tolerate

# arg[1] = pv designation
# arg[2] = 'y' or 'n' to push slope directly to PVs. This is gross, change to a switch

if len(sys.argv) != 3:
    print(f'{sys.argv[0]} expects 2 arguments')
    exit(0)

chassis_id = sys.argv[1]
outfile = f'{calib_date}_{file_time}_cal_{chassis_id}_bipolar'

dmm_sock = socket(AF_INET, SOCK_STREAM)
dmm_sock.connect((dmm_ip, dmm_port))
afg_sock = socket(AF_INET, SOCK_STREAM)
afg_sock.connect((afg_ip, afg_port))

# Grab DMM info
dmm_sock.send(cmd_idn)
msg = dmm_sock.recv(1024).decode().strip().split(',')
print(msg)
dmm_mfr = msg[0]
dmm_mdl = msg[1]
dmm_sn = msg[2]
dmm_fw = msg[3]
print(f'dmm info : {dmm_mfr} | {dmm_mdl} | {dmm_sn} | {dmm_fw}')

# Print AFG info
afg_sock.send(cmd_idn)
msg = afg_sock.recv(1024)
print(f'afg info : {msg.decode().strip()}')
time.sleep(.5)

# Set AFG to positive voltage
afg_sock.send(cmd_set_pos)
time.sleep(.5)

channel_list = []
dmm_list = []
adc_list = []
calib_pairs = []
# Generate a pv list
for i in range(num_adc_channels):
    channel_list.append(f'FDAS:{chassis_id}:SA:Ch{i+1:02d}_')

try:
    r_file = open(f'{outfile}_raw.csv', 'w')
except Exception as e:
        print(f'Error writing file: {e}')
        exit(-1)

r_file.write(f'[BIPOLAR MODE] Calibrating chassis: {chassis_id}\n')
r_file.write(f'Date and time: {calib_date} {calib_time}\n')
r_file.write(f'DMM Model: {dmm_mfr} {dmm_mdl}\n')
r_file.write(f'DMM serial number: {dmm_sn}\n')
r_file.write(f'DMM firmware ver : {dmm_fw}\n')
r_file.write('ch, volts, waveform\n')
i = 0
for channel in channel_list:
    print(f'\n\n---------------\nCalibrating {channel}:')
    ch_pass = 'Pass'
    afg_sock.send(cmd_set_pos)  # Set function generator to positive voltage
    # Wait until adc shows proper sign
    wait_timer = 0
    while float(caget(channel).mean(0)) < 4000:
        time.sleep(.2)
        wait_timer += 1
        if wait_timer > 50: # Wait up to 10 seconds
            print(f'ADC input not high enough - is the cable connected?')
            ch_pass = 'FAIL'
            break
    time.sleep(1)
    dmm_sock.send(cmd_read)
    dmm_p = float(dmm_sock.recv(1024).decode().strip())
    if not math.isfinite(dmm_p):
        print(f'**ERROR Reading voltage**\n** CALIBRATION STOPPED **\n** Chassis {chassis_id} NOT calibrated')
        ch_pass = 'FAIL'
    adc_wave_p = caget(channel)
    if not math.isfinite(adc_wave_p.mean(0)):
        print(f'**ERROR Reading waveform**\n** CALIBRATION STOPPED **\n** Chassis {chassis_id} NOT calibrated')
        ch_pass = 'FAIL'
    dmm_sock.send(cmd_read)
    dmm_validate = float(dmm_sock.recv(1024).decode().strip())
    if abs(dmm_p-dmm_validate) > dmm_deadband:
        print(f'DMM readback discrepency ({dmm_p} vs {dmm_validate}) on channel: {channel}')
        ch_pass = 'FAIL'

    ## Switch to negative
    afg_sock.send(cmd_set_neg)  # Set function generator to negative voltage
    # Wait until adc shows proper sign
    while float(caget(channel).mean(0)) > 4000:
        time.sleep(.2)
        wait_timer += 1
        if wait_timer > 50: # Wait up to 10 seconds
            print(f'ADC input not low enough - is the cable connected?')
            ch_pass = 'FAIL'
            break
    time.sleep(1)
    dmm_sock.send(cmd_read)
    dmm_n = float(dmm_sock.recv(1024).decode().strip())
    if not math.isfinite(dmm_n):
        print(f'**ERROR Reading voltage**\n** CALIBRATION STOPPED **\n** Chassis {chassis_id} NOT calibrated')
        ch_pass = 'FAIL'

    adc_wave_n = caget(channel)
    if not math.isfinite(adc_wave_n.mean(0)):
        print(f'**ERROR Reading waveform**\n** CALIBRATION STOPPED **\n** Chassis {chassis_id} NOT calibrated')
        ch_pass = 'FAIL'
    dmm_sock.send(cmd_read)
    dmm_validate = float(dmm_sock.recv(1024).decode().strip())
    if abs(dmm_n-dmm_validate) > dmm_deadband:
        print(f'DMM readback discrepency ({dmm_n} vs {dmm_validate}) on channel: {channel}')
        ch_pass = 'FAIL'

    # Calculate slope and intercept 
    x1 = float(adc_wave_p.mean(0))
    x2 = float(adc_wave_n.mean(0))
    y1 = dmm_p
    y2 = dmm_n
    slope, intercept  = numpy.polyfit([x1, x2], [y1, y2], 1)

    # Boundary checks
    if abs(float(adc_wave_p.mean(0))) - expected_count > count_tolerance:
        print(f'adc_wave_p boundary violation')
        ch_pass = 'FAIL'
    elif abs(float(adc_wave_n.mean(0))) - expected_count > count_tolerance:
        print(f'adc_wave_n boundary violation')
        ch_pass = 'FAIL'
    elif abs(intercept) > offset_tolerance:
        print(f'offset boundary violation')
        print(f'intercept = {intercept}')
        ch_pass = 'FAIL'
    elif abs(slope - expected_slope) > slope_tolerance:
        print(f'slope boundary violation')
        print(f'slope = {slope}')
        ch_pass = 'FAIL'

    if sys.argv[2] == 'y' or sys.argv[2] == 'Y':  # User requested pushing data to PVs 
        time.sleep(.1)
        caput(f'FDAS:{chassis_id}:SA:Ch{i+1:02d}:ASLO', slope)
        caput(f'FDAS:{chassis_id}:SA:Ch{i+1:02d}:AOFF', intercept)
        # Write time-stamp to PV for last calibration, or zero if calibration failed
        if ch_pass != 'FAIL':
            caput(f'FDAS:{chassis_id}:SA:Ch{i+1:02d}:TCAL', time.time())
        else:
            caput(f'FDAS:{chassis_id}:SA:Ch{i+1:02d}:TCAL', 0)

    calib_pairs.append((dmm_p, dmm_n, float(adc_wave_p.mean(0)), float(adc_wave_n.mean(0)), slope, intercept, ch_pass))
    i += 1
    r_file.write(f'{i}, {dmm_p}')
    for m in adc_wave_p:
        r_file.write(f', {m}')
    r_file.write(f'\n')
    r_file.write(f'{i}, {dmm_n}')
    for m in adc_wave_n:
        r_file.write(f', {m}')
    r_file.write(f'\n')
    time.sleep(.1)
    if ch_pass != 'Pass':
        overall_pass = False
    print(f'Result: {ch_pass}')
r_file.close()

# Write summary file
try:
    with open(f'{outfile}_calc.csv', 'w') as o_file:
        o_file.write(f'[BIPOLAR MODE] Calibrating chassis: {chassis_id}\n')
        o_file.write(f'Date and time: {calib_date} {calib_time}\n')
        o_file.write(f'DMM Model: {dmm_mfr} {dmm_mdl}\n')
        o_file.write(f'DMM serial number: {dmm_sn}\n')
        o_file.write(f'DMM firmware ver : {dmm_fw}\n')
        o_file.write(f'ch, volts_1, volts_2, counts_1, counts_2,  slope, offset, result\n')
        i = 0
        for elem in calib_pairs:
            i+=1
            o_file.write(f'{i}')
            for member in elem:
                o_file.write(f',{member}')
            o_file.write(f'\n')
except Exception as e:
        print(f'Error writing file: {e}')
o_file.close()
afg_sock.send(cmd_set_zero)
if overall_pass:
    print(f'Calibration of {chassis_id} passed on {calib_date} at {calib_time}')
else:
    print('***************')
    print(f'Calibration of {chassis_id} FAILED on {calib_date} at {calib_time}')
    print('***************')

