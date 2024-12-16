import cothread
from cothread.catools import *
from datetime import date, datetime
import math
import numpy
from socket import *
from softioc import softioc, builder, alarm
import sys
import time

#dmm_ip = '192.168.83.47'       # ATF equipment
#afg_ip = '192.168.83.48'       # ATF equipment
dmm_ip = '192.168.79.97'        # Development equipment
afg_ip = '192.168.79.98'        # Development equipment
dmm_port = 5025
afg_port = 5025

num_adc_channels = 32

def pvreport(time_pv, status_pv, message):
    current_date = date.today()
    current_time = datetime.now().strftime('%H:%M:%S')
    time_pv.set(f'{current_date} {current_time}')
    try:
        status_pv.set(message[:39])

    except Exception as e:
        print(f'Found exception {e} - in pvreport function. Terminating IOC')
        exit(-1)


def runcalibration(chassis_id, push_data, time_pv, message_pv, pv_status_leds):
    cmd_idn = b'*IDN?\n'
    cmd_read = b'READ?\n'
    cmd_set_pos = b'APPL:DC DEF, DEF, 9.0\n'  # +9V
    cmd_set_neg = b'APPL:DC DEF, DEF, -9.0\n'  # -9V
    cmd_set_zero = b'APPL:DC DEF, DEF, 0\n'  # 0V

    dmm_deadband = 0.01
    calib_date = date.today()
    calib_time = datetime.now().strftime('%H:%M:%S')
    file_time = datetime.now().strftime('%H-%M-%S')

    overall_pass = True  # Goes False if any channel fails calibration

    expected_count = 5560000  # Approx count value for 9v input
    count_tolerance = 10000  # number of counts difference we tolerate
    offset_tolerance = .01  # largest offset we tolerate (we expect zero as offset)
    expected_slope = 1.6e-6  # Expected slope values based on previous runs
    slope_tolerance = 1e-6  # largest slope variation we tolerate

    outfile = f'{calib_date}_{file_time}_cal_{chassis_id}_bipolar'
    dmm_sock = socket(AF_INET, SOCK_STREAM)
    dmm_sock.connect((dmm_ip, dmm_port))
    afg_sock = socket(AF_INET, SOCK_STREAM)
    afg_sock.connect((afg_ip, afg_port))

    for pv in pv_status_leds:
        pv.set(1)

    # Grab DMM info
    dmm_sock.send(cmd_idn)
    msg = dmm_sock.recv(1024).decode().strip().split(',')
    dmm_mfr = msg[0]
    dmm_mdl = msg[1]
    dmm_sn = msg[2]
    dmm_fw = msg[3]
    #print(f'dmm info : {dmm_mfr} | {dmm_mdl} | {dmm_sn} | {dmm_fw}')

    # grab AFG info
    afg_sock.send(cmd_idn)
    msg = afg_sock.recv(1024)
    #print(f'afg info : {msg.decode().strip()}')
    time.sleep(.5)

    # Set AFG to positive voltage
    afg_sock.send(cmd_set_pos)
    time.sleep(.5)

    channel_list = []
    dmm_list = []
    adc_list = []
    calib_pairs = []
    ch_pos = []
    ch_neg = []
    dmm_pos = []
    dmm_neg = []

    # Generate a pv list
    for i in range(num_adc_channels):
        channel_list.append(f'FDAS:{chassis_id}:SA:Ch{i + 1:02d}_')

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

    # Set to positive voltage
    pvreport(time_pv, message_pv, 'Sourcing positive voltage')
    # print('Switching to positive excitation voltage')
    afg_sock.send(cmd_set_pos)  # Set function generator to positive voltage
    # Wait until adc shows proper sign
    wait_timer = 0
    while float(caget(channel_list[0]).mean(0)) < 4000:
        time.sleep(.2)
        wait_timer += 1
        if wait_timer > 50:  # Wait up to 10 seconds
            pvreport(time_pv, message_pv, '**ABORT**: ADC positive input not high enough - check cable')
            ch_pass = 'FAIL'
            break
    time.sleep(1)

    for channel in channel_list:
        ch_pass = 'Pass'
        ch_id = channel_list.index(channel)+1
        dmm_sock.send(cmd_read)
        dmm_p = float(dmm_sock.recv(1024).decode().strip())
        pv_status_leds[ch_id-1].set(1)
        if not math.isfinite(dmm_p):
            pvreport(time_pv, message_pv,f'**ABORT** error reading voltage: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        adc_wave_p = caget(channel)
        if not math.isfinite(adc_wave_p.mean(0)):
            pvreport(time_pv, message_pv,f'**ABORT** error reading waveform: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        dmm_sock.send(cmd_read)
        dmm_validate = float(dmm_sock.recv(1024).decode().strip())
        if abs(dmm_p - dmm_validate) > dmm_deadband:
            pvreport(time_pv, message_pv,
                     f'**ABORT** dmm ({dmm_p} vs {dmm_validate}) on: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        dmm_pos.append(dmm_p)
        ch_pos.append(adc_wave_p)
        pvreport(time_pv, message_pv, f'Recording ch {ch_id} positive voltage')
        pv_status_leds[ch_id-1].set(3)
        time.sleep(.1)
    time.sleep(1)

    pvreport(time_pv, message_pv, 'Sourcing negative voltage')
    time.sleep(.5)

    for pv in pv_status_leds:
        pv.set(1)
    ## Switch to negative
    afg_sock.send(cmd_set_neg)  # Set function generator to negative voltage
    # Wait until adc shows proper sign
    while float(caget(channel).mean(0)) > 4000:
        time.sleep(.2)
        wait_timer += 1
        if wait_timer > 50:  # Wait up to 10 seconds
            pvreport(time_pv, message_pv, '**ABORT**: ADC negative input not high enough - check cable')
            ch_pass = 'FAIL'
        break
    time.sleep(1)

    for channel in channel_list:
        ch_id = channel_list.index(channel)+1
        dmm_sock.send(cmd_read)
        dmm_n = float(dmm_sock.recv(1024).decode().strip())
        if not math.isfinite(dmm_n):
            pvreport(time_pv, message_pv,f'**ABORT** error reading voltage: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        adc_wave_n = caget(channel)
        if not math.isfinite(adc_wave_n.mean(0)):
            pvreport(time_pv, message_pv,f'**ABORT** error reading waveform: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        dmm_sock.send(cmd_read)
        dmm_validate = float(dmm_sock.recv(1024).decode().strip())
        if abs(dmm_n - dmm_validate) > dmm_deadband:
            pvreport(time_pv, message_pv,
                     f'**ABORT** dmm ({dmm_n} vs {dmm_validate}) on: {chassis_id}, ch {ch_id}')
            ch_pass = 'FAIL'
            pv_status_leds[ch_id - 1].set(2)
            break
        dmm_neg.append(dmm_n)
        ch_neg.append(adc_wave_n)
        pvreport(time_pv, message_pv, f'Channel {ch_id} negative voltage acquired')
        pv_status_leds[ch_id-1].set(3)

    pvreport(time_pv, message_pv, 'Calculating slope and offset')

    # Calculate linear fit for each channel
    for ch in range(0, len(channel_list)):
        # Calculate slope and intercept
        x1 = float(ch_pos[ch].mean(0))
        x2 = float(ch_neg[ch].mean(0))
        y1 = dmm_pos[ch]
        y2 = dmm_neg[ch]
        slope, intercept = numpy.polyfit([x1, x2], [y1, y2], 1)

        # Boundary checks
        if abs(float(adc_wave_p.mean(0))) - expected_count > count_tolerance:
            pvreport(time_pv, message_pv, f'adc_wave_p boundary violation')
            ch_pass = 'FAIL'
        elif abs(float(adc_wave_n.mean(0))) - expected_count > count_tolerance:
            pvreport(time_pv, message_pv, f'adc_wave_n boundary violation')
            ch_pass = 'FAIL'
        elif abs(intercept) > offset_tolerance:
            pvreport(time_pv, message_pv, f'intercept boundary violation = {intercept}')
            ch_pass = 'FAIL'
        elif abs(slope - expected_slope) > slope_tolerance:
            pvreport(time_pv, message_pv, f'slope boundary violation = {slope}')
            ch_pass = 'FAIL'

        if push_data:  # User requested pushing data to PVs
            pvreport(time_pv, message_pv,'Updating slope and offset on PVs')
            time.sleep(.1)
            caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:ASLO', slope)
            caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:AOFF', intercept)
            # Write time-stamp to PV for last calibration, or zero if calibration failed
            if ch_pass != 'FAIL':
                caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:TCAL', time.time())
            else:
                caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:TCAL', 0)

        calib_pairs.append(
            (dmm_p, dmm_n, float(adc_wave_p.mean(0)), float(adc_wave_n.mean(0)), slope, intercept, ch_pass))
        r_file.write(f'{ch + 1}, {dmm_p}')
        for m in adc_wave_p:
            r_file.write(f', {m}')
        r_file.write(f'\n')
        r_file.write(f'{ch + 1}, {dmm_n}')
        for m in adc_wave_n:
            r_file.write(f', {m}')
        r_file.write(f'\n')
        time.sleep(.1)
        if ch_pass != 'Pass':
            overall_pass = False
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
                i += 1
                o_file.write(f'{i}')
                for member in elem:
                    o_file.write(f',{member}')
                o_file.write(f'\n')
    except Exception as e:
        print(f'Error writing file: {e}')
    o_file.close()
    afg_sock.send(cmd_set_zero)

    if overall_pass:
        # print(f'Calibration of chassis {chassis_id} passed on {calib_date} at {calib_time}')
        return('Success')
    else:
        #print('***************')
        #print(f'Calibration of chassis {chassis_id} FAILED on {calib_date} at {calib_time}')
        #print('***************')
        return('Failed')


if __name__ == '__main__':
    # Set up pvs
    init_date = date.today()
    init_time = datetime.now().strftime('%H:%M:%S')
    builder.SetDeviceName('FDAS:Calib')
    pv_status_leds = []
    pv_chassis_id = builder.aOut('chassisID', initial_value=1)
    pv_start_calibration = builder.boolOut('start', initial_value=0)
    pv_status_timedate = builder.stringOut('status_time', initial_value=f'{init_date} {init_time}')
    pv_status_message = builder.stringOut('status_message', initial_value='IOC startup')
    pv_test_mode = builder.boolOut('testmode', initial_value=1)
    for i in range(32):
        # 0 = unk, 1 = calibrating, 2 = fail, 3 = pass
        pv_status_leds.append(builder.aIn(f'stat{i+1}', initial_value=0))

    # set up the ioc
    builder.LoadDatabase()
    softioc.iocInit()


    def update():
        for pv in pv_status_leds:
            pv.set(0)
        while True:
            if pv_start_calibration.get() != 0:
                pv_start_calibration.set(0)
                calib_date = date.today()
                chassis_id = int(pv_chassis_id.get())
                pvreport(pv_status_timedate, pv_status_message, f'Beginning cal of chassis {chassis_id}')
                if pv_test_mode.get() != 1:
                    push_data = True
                else:
                    push_data = False
                rval = runcalibration(f'{chassis_id:02}', push_data, pv_status_timedate, pv_status_message,
                                      pv_status_leds)
                end_time = datetime.now().strftime('%H:%M:%S')
                pv_status_timedate.set(f'{calib_date} {end_time}')
                pvreport(pv_status_timedate, pv_status_message,
                         f'Chassis {chassis_id:02} calibration result : {rval}')
            cothread.Sleep(1)


    cothread.Spawn(update)
    try:
        softioc.interactive_ioc(globals())

    except Exception as e:
        print(f'Found exception {e} - terminating')
        exit(-1)
