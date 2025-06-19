import argparse
import cothread
from p4p.client.cothread import Context
from datetime import datetime
import math
import numpy
import socket
from softioc import softioc, builder, alarm
import os
import logging
from collections import namedtuple

_log = logging.getLogger(__name__)
log_info = False

PerChanT = namedtuple('PerChan', [
    'PrevTCAL', 'PrevSlope', 'PrevOff',
    'NewADC1', 'NewADC2',
    'NewStatus', 'NewSlope', 'NewOff', 'NewSlopeDiff', 'NewOffDiff',
])

# ATF equipment
dmm_addr = ('192.168.83.47', 5025)
afg_addr = ('192.168.83.48', 5025)
# Development equipment
#dmm_addr = ('192.168.79.97', 5025)
#afg_addr = ('192.168.79.98', 5025)

cmd_idn = b'*IDN?\n'
cmd_read = b'READ?\n'
cmd_set_pos = b'APPL:DC DEF, DEF, 9.0\n'  # +9V
cmd_set_neg = b'APPL:DC DEF, DEF, -9.0\n'  # -9V
cmd_set_zero = b'APPL:DC DEF, DEF, 0\n'  # 0V
cmd_query_state = b'APPL?\n'
# reply: "DC +1.0E+03,+2.000E-01,+0.00E+00"
cmd_set_highz = b'OUTPUT1:LOAD INF\n'

num_adc_channels = 32

ctxt = Context(nt=False)  # PVA client

def report_status(message):
    _log.info('STATUS: %s', message)
    now = datetime.now()
    pv_status_timedate.set(now.strftime('%Y/%m/%d %H:%M:%S'))
    pv_status_message.set(message[:39])

class CalibProcess:
    expected_count = 5560000  # Approx count value for 9v input
    count_tolerance = 10000  # number of counts difference we tolerate
    offset_tolerance = .01  # largest offset we tolerate (we expect zero as offset)
    expected_slope = 1.6e-6  # Expected slope values based on previous runs
    slope_tolerance = 1e-6  # largest slope variation we tolerate
    settle_slope = 1e-3  # Largest slope magnitude we can tolerate for settling purposes

    dmm_deadband = 0.01

    def __init__(self, chassis):
        self.chassis = chassis
        self.now = datetime.now()

        # Find acquisition rate in samples per second
        self.daq_rate = int(ctxt.get('FDAS:ACQ:rate.RVAL').value)

    def connect_dmm_afg(self):
        self.dmm_sock = socket.create_connection(dmm_addr)
        self.dmm_rx = self.dmm_sock.makefile('rb', buffering=0)
        self.afg_sock = socket.create_connection(afg_addr)
        self.afg_rx = self.afg_sock.makefile('rb', buffering=0)


        # Grab DMM info
        self.dmm_sock.sendall(cmd_idn)
        msg = self.dmm_rx.readline(1024).decode().strip()
        _log.debug('DMM info %r', msg)
        self.dmm_mfr, self.dmm_mdl, self.dmm_sn, self.dmm_fw = msg.split(',')[:4]

        # grab AFG info
        self.afg_sock.sendall(cmd_idn)
        msg = self.afg_rx.readline(1024)
        _log.debug('DMM info %r', msg)

        # Zero AFG and set to high Z output
        self.afg_sock.sendall(cmd_set_zero)
        self.afg_sock.sendall(cmd_set_highz)

        pv_dmm_manu.set(self.dmm_mfr)
        pv_dmm_modl.set(self.dmm_mdl)
        pv_dmm_fw.set(self.dmm_fw)
        pv_dmm_sn.set(self.dmm_sn)

    def set_afg(self, v):
        self.afg_sock.sendall(f'APPL:DC DEF, DEF, {v:.1f}\n'.encode())
        rbV = self._query_afg()
        assert abs(v-rbV)<0.001, (v, rbV)

    def _query_afg(self):
        self.afg_sock.sendall(cmd_query_state)
        msg = self.afg_rx.readline(1024).strip().strip(b'"')
        # '"DC +1.0E+03,+2.000E-01,+0.00E+00"\n'
        assert msg.startswith(b'DC '), (type(msg), msg)
        parts = msg[3:].split(b',')
        return float(parts[2])

    def dmm_read(self):
        vals = [None, None]
        for i in range(2):
            self.dmm_sock.sendall(cmd_read)
            vals[i] = float(self.dmm_rx.readline(1024).decode().strip())
            if not math.isfinite(vals[i]):
                raise RuntimeError('DMM readout error.  Unable to proceed')

        if numpy.abs(numpy.diff(vals))[0] > self.dmm_deadband:
            raise RuntimeError('DMM does not settle.  Unable to proceed')
        return vals[1]

    # Wait for adc to settle at desired range
    def query_adc(self):
        updateQ = cothread.EventQueue(max_length=16)
        with ctxt.monitor(f'FDAS:{self.chassis:02d}:SA:V', updateQ.Signal):
            updateQ.Wait(timeout=5) # wait for and discard initial update

            for n in range(10):  # Allow 10 chances to converge
                report_status(f'Wait for settle {n}')

                grp = updateQ.Wait(timeout=15.0) # slowest update rate is 1k
                tbase = grp['value.T']
                traces = numpy.concatenate(
                    [grp.value[f'Ch{ch + 1:02d}'][:, None] for ch in range(num_adc_channels)],
                    axis=1
                )
                assert tbase.ndim==1, tbase.shape
                assert traces.ndim==2 and traces.shape[1]==32, traces.shape

                # will declare success if any one channel is settled
                for ch in range(32):
                    chanData = traces[:,ch]
                    # nominal +- 9V is +-5560000 count
                    if numpy.abs(chanData).min() < 5000000:
                        _log.debug('ch %d too low %f', ch+1, numpy.abs(chanData).min())
                        continue

                    elif numpy.abs(chanData).max() > 6000000:
                        _log.debug('ch %d too high %f', ch+1, numpy.abs(chanData).max())
                        continue

                    slope, offset = numpy.polyfit(tbase, chanData, 1)
                    # slope V/sec
                    _log.debug('ch %d : %f + %f *t', ch+1, offset, slope)

                    if abs(slope) > 100.0: # V/sec
                        _log.debug('ch %d too slope %f', ch+1, abs(slope))
                        continue

                    report_status(f'Ch {ch} settled {n}')
                    ret = traces.mean(0)
                    assert ret.shape==(32,), ret.shape
                    return ret

            raise RuntimeError('Not settled')

    def compute_calib(self, dmmPos, dmmNeg, adcPos, adcNeg):
        Y = numpy.asarray([dmmPos, dmmNeg])
        pv_dmm[0].set(Y[0])
        pv_dmm[1].set(Y[1])

        for ch, chan in enumerate(pv_chan):
            X = numpy.asarray([adcPos[ch], adcNeg[ch]])
            chan.NewADC1.set(int(X[0]))
            chan.NewADC2.set(int(X[1]))

            _log.debug('ch %d fit X %r Y %r', ch+1, X, Y)
            slope, offset = numpy.polyfit(X, Y, 1) # linear fit

            prevSlope = chan.PrevSlope.get()
            diffSlope = (slope - prevSlope) / numpy.asarray(prevSlope) # return Inf if prev==0

            prevOff = chan.PrevOff.get()
            diffOff = (offset - prevOff) / numpy.asarray(prevOff) # return Inf if prev==0

            chan.NewOff.set(offset)
            chan.NewOffDiff.set(diffOff)
            chan.NewSlope.set(slope)
            chan.NewSlopeDiff.set(diffSlope)

            chPass = True

            if ( numpy.abs(numpy.abs(X) - self.expected_count) > self.count_tolerance ).any():
                _log.info('Chan %d ADC values out of range %r', ch+1, X)
                chPass = False

            if abs(offset) > self.offset_tolerance:
                _log.info('Chan %d offset too large %r', ch+1, offset)
                chPass = False

            if abs(slope - self.expected_slope) > self.slope_tolerance:
                _log.info('Chan %d slope out of range %r', ch+1, slope)
                chPass = False

            chan.NewStatus.set(3 if chPass else 2)

    def write_raw(self, dmmPos, dmmNeg, adcPos, adcNeg):
        'Write "raw" calibration file'
        # cal-20250323/2025-03-23_10-14-15_cal_05_bipolar_raw.csv
        outdir = self.now.strftime('cal-%Y%m%d')
        outname = self.now.strftime('%Y-%m-%d_%H-%M-%S')
        outname = f'{outdir}/{outname}_cal_{self.chassis:02d}_bipolar_raw.csv'

        if not os.path.exists(outdir):
            os.mkdir(outdir)

        with open(outname, 'w') as F:
            F.write(f'''\
[BIPOLAR MODE] Calibrating chassis: {self.chassis}
Date and time: {self.now.strftime('%Y-%m-%d')} {self.now.strftime('%H:%M:%S')}
DMM Model: {self.dmm_mfr} {self.dmm_mdl}
DMM serial number: {self.dmm_sn}
DMM firmware ver : {self.dmm_fw}
Operator : {pv_oper.get() or 'Unspecified'}
ch, volts, waveform
''')

            for ch in range(1, 33):
                F.write(f'{ch}, {dmmPos}')
                for adc in adcPos:
                    F.write(f', {adc}')
                F.write(f'\n{ch}, {dmmNeg}')
                for adc in adcNeg:
                    F.write(f', {adc}')
                F.write('\n')

def reset_status():
    _log.info('Reset status')
    pv_status.set(0)
    pv_dmm_manu.set('')
    pv_dmm_modl.set('')
    pv_dmm_fw.set('')
    pv_dmm_sn.set('')
    pv_adc_sn.set('')
    for chan in pv_chan:
        chan.NewStatus.set(0)
        chan.NewSlope.set(math.nan)
        chan.NewSlopeDiff.set(math.nan)
        chan.NewOff.set(math.nan)
        chan.NewOffDiff.set(math.nan)

def update_chassis_sel():
    reset_status()
    chassis = pv_chassis_sel.get()
    _log.info('Chassis %d selected', chassis)

    pv_adc_sn.set_field('DOL', f'FDAS:{chassis:02d}:Quartz:serno CP MSS')

    # setup links to previous
    for i,chan in enumerate(pv_chan, 1):
        chan.PrevTCAL.set_field('DOL', f'FDAS:{chassis:02d}:SA:Ch{i:02d}:LASTCAL CP MSS')
        chan.PrevSlope.set_field('DOL', f'FDAS:{chassis:02d}:SA:Ch{i:02d}:ASLO CP MSS')
        chan.PrevOff.set_field('DOL', f'FDAS:{chassis:02d}:SA:Ch{i:02d}:AOFF CP MSS')

    pv_chassis_rb.set(chassis)

def run_calibration(chassis):
    reset_status()
    pv_status.set(1)
    try:
        _log.info('Chassis %d start calibration', chassis)

        for chan in pv_chan:
            chan.NewStatus.set(1) # Running

        C = CalibProcess(chassis)
        C.connect_dmm_afg()

        C.set_afg(9.0)
        adcPos = C.query_adc() # [32]
        dmmPos = C.dmm_read() # scalar

        C.set_afg(-9.0)
        adcNeg = C.query_adc() # [32]
        dmmNeg = C.dmm_read() # scalar

        C.compute_calib(dmmPos, dmmNeg, adcPos, adcNeg)
        C.write_raw(dmmPos, dmmNeg, adcPos, adcNeg)
    except:
        pv_status.set(2)
        raise
    else:
        pv_status.set(3)
        report_status('Success')
    finally:
        _log.debug('End Chassis %d start calibration', chassis)

def write_calib(chassis, now):
    'Write "raw" calibration file'

    if pv_dmm_modl.get()=='':
        raise RuntimeError('No calib to write')

    # cal-20250310/2025-03-23_10-27-28_cal_05_bipolar_calc.csv
    outdir = now.strftime('cal-%Y%m%d')
    outname = now.strftime('%Y-%m-%d_%H-%M-%S')
    outname = f'{outdir}/{outname}_cal_{chassis:02d}_bipolar_calc.csv'

    samp_rate = CalibProcess(chassis).daq_rate
    oper = pv_oper.get() or 'Unspecified'

    if not os.path.exists(outdir):
        os.mkdir(outdir)

    with open(outname, 'w') as F:
        F.write(f'''\
[BIPOLAR MODE] Calibrating chassis: {chassis}
Date and time: {now.strftime('%Y-%m-%d')} {now.strftime('%H:%M:%S')}
DMM Model: {pv_dmm_manu.get()} {pv_dmm_modl.get()}
DMM serial number: {pv_dmm_sn.get()}
DMM firmware ver : {pv_dmm_fw.get()}
Operator : {oper}
time, oper_id, samp_rate, adc_serno, dmm_mfr, dmm_sn, dmm_fw, chassis, ch, volts_1, volts_2, counts_1, counts_2,  slope, offset, result
''')

        for n, chan in enumerate(pv_chan, 1):
            row = [
                now.timestamp(),
                f'"{oper}"',
                samp_rate,
                pv_adc_sn.get(),
                f'"{pv_dmm_manu.get()}"',
                f'"{pv_dmm_sn.get()}"',
                f'"{pv_dmm_fw.get()}"',
                chassis,
                n,
                pv_dmm[0].get(),
                pv_dmm[1].get(),
                chan.NewADC1.get(),
                chan.NewADC2.get(),
                chan.NewSlope.get(),
                chan.NewOff.get(),
                'Pass' if chan.NewStatus.get()==3 else 'FAIL',
            ]
            print(*row, sep=',', file=F)

def put_calib(chassis, now):
    tcal = now.timestamp() # cal time in POSIX seconds
    updates = []

    for n, chan in enumerate(pv_chan, 1):
        offset, slope, status = chan.NewOff.get(), chan.NewSlope.get(), chan.NewStatus.get()
        updates += [
            (f'FDAS:{chassis:02d}:SA:Ch{n:02d}:ASLO', slope),
            (f'FDAS:{chassis:02d}:SA:Ch{n:02d}:AOFF', offset),
            (f'FDAS:{chassis:02d}:SA:Ch{n:02d}:TCAL', tcal if status==3 else 0),
        ]

    for name, val in updates:
        print('PUT', name, val)

    ctxt.put(
        [name for name, _val in updates],
        [val for _name, val in updates],
    )

def commit_calibration(chassis):
    if pv_status.get()!=3:
        report_status('No calib to commit')
        return
    now = datetime.now() # commit time, not calib time...
    write_calib(chassis, now)
    put_calib(chassis, now)

def getargs():
    parser = argparse.ArgumentParser(description='ATF Calibration IOC')
    parser.add_argument('-f', '--faddr', help='Specify function generator IP addr')
    parser.add_argument('-fp', '--fport', help='Specify function generator SCPI port')
    parser.add_argument('-v', '--vaddr', help='Specify voltmeter IP addr')
    parser.add_argument('-vp', '--vport', help='Specify voltmeter SCPI port')
    parser.add_argument('--prefix', default='FDAS:Calib',
                        help='PV name prefix')
    return parser

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    args = getargs().parse_args()

    if args.faddr:
        afg_ip = args.faddr
    else:
        afg_ip = afg_addr[0]
    if args.fport:
        afg_port = args.fport
    else:
        afg_port = afg_addr[1]

    if args.vaddr:
        dmm_ip = args.vaddr
    else:
        dmm_ip = dmm_addr[0]
    if args.vport:
        dmm_port = args.vport
    else:
        dmm_port = dmm_addr[1]

    afg_addr = (afg_ip, afg_port)
    dmm_addr = (dmm_ip, dmm_port)

    # Set up pvs
    builder.SetDeviceName(args.prefix)

    action_request = cothread.Event()
    def wakeup(_val):
        _log.debug('wakeup')
        action_request.Signal()

    # common records
    pv_oper = builder.stringOut('oper')

    pv_status = builder.mbbIn('status',
                              'Idle',
                              ('Run', alarm.MINOR_ALARM),
                              ('Error', alarm.MAJOR_ALARM),
                              'Done'
    )
    pv_dmm_manu = builder.stringIn('dmm:manu')
    pv_dmm_modl = builder.stringIn('dmm:model')
    pv_dmm_fw = builder.stringIn('dmm:fw')
    pv_dmm_sn = builder.stringIn('dmm:serno')
    pv_adc_sn = builder.stringOut('adc:serno', OMSL='closed_loop')

    def validate_chassis(pv, val):
        return val in range(1, 33)
    pv_chassis_sel = builder.longOut('chassis:sel', initial_value=1, validate=validate_chassis,
                                     on_update=wakeup)
    pv_chassis_rb = builder.longOut('chassis:cur', initial_value=0)
    pv_start_calibration = builder.boolOut('start', initial_value=0,
                                           on_update=wakeup,
                                           ZNAM='Idle', ONAM='Run')
    pv_commit_calibration = builder.boolOut('commit', initial_value=0,
                                            on_update=wakeup,
                                           ZNAM='Idle', ONAM='Run')
    pv_status_timedate = builder.stringOut('status_time')
    pv_status_message = builder.stringOut('status_message', initial_value='Startup')
    pv_dmm = (
        builder.aIn('dmm:1'),
        builder.aIn('dmm:2'),
    )

    # per-channel records
    pv_chan = []
    for chan in range(1, 33):
        pv_chan.append(PerChanT(
            PrevTCAL=builder.stringOut(f'ch{chan:02d}:prev:TCAL', OMSL='closed_loop'),
            PrevSlope=builder.aOut(f'ch{chan:02d}:prev:slope', OMSL='closed_loop', PREC=6),
            PrevOff=builder.aOut(f'ch{chan:02d}:prev:offset', OMSL='closed_loop', PREC=2),
            NewStatus=builder.mbbIn(f'ch{chan:02d}:new:status',
                                 'Idle',
                                 ('Run', alarm.MINOR_ALARM),
                                 ('Fail', alarm.MAJOR_ALARM),
                                 'Pass',
            ),
            NewADC1=builder.longIn(f'ch{chan:02d}:new:adc:1'),
            NewADC2=builder.longIn(f'ch{chan:02d}:new:adc:2'),
            NewSlope=builder.aIn(f'ch{chan:02d}:new:slope', PREC=6),
            NewOff=builder.aIn(f'ch{chan:02d}:new:slope:diff', PREC=3, EGU='%'),
            NewSlopeDiff=builder.aIn(f'ch{chan:02d}:new:offset', PREC=2),
            NewOffDiff=builder.aIn(f'ch{chan:02d}:new:offset:diff', PREC=3, EGU='%'),
        ))

    # set up the ioc
    builder.LoadDatabase()
    softioc.iocInit()

    def loop():
        _log.debug('loop start')
        while True:
            action_request.Wait()
            _log.debug('loop wake')
            try:
                if pv_chassis_sel.get() != pv_chassis_rb.get():
                    update_chassis_sel()

                chassis = pv_chassis_rb.get()

                if pv_start_calibration.get():
                    pv_start_calibration.set(0)
                    run_calibration(chassis)

                if pv_commit_calibration.get():
                    pv_commit_calibration.set(0)
                    commit_calibration(chassis)
            except SystemExit:
                pass
            except Exception as e:
                _log.exception('Unexpected')
                reset_status()
                report_status(f'EXC: {e!r}')
                cothread.Sleep(10) # throttle log spam

    action_request.Signal() # trigger initial chassis select
    cothread.Spawn(loop)

    try:
        softioc.interactive_ioc(globals())

    except Exception as e:
        _log.exception('Unhandled exception: %s', e)
        exit(-1)
