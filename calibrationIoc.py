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
    'NewStatus', 'NewSlope', 'NewOff',
])

num_adc_channels = 32

def per_err(expected:float, actual:float): # return fraction
    return (actual-expected)/expected

def report_status(message):
    _log.info('STATUS: %s', message)
    now = datetime.now()
    pv_status_timedate.set(now.strftime('%Y/%m/%d %H:%M:%S'))
    pv_status_message.set(message[:39])

class CalibProcess:
    offset_tolerance = .01 # [V] largest offset we tolerate (we expect zero as offset)
    slope_tolerance = 2.0  # [%] allowed gain deviation from nominal

    # Nominal scaling from ADC counts to volts in terms of op-amp gain set resister (R_g)
    # and ADC reference voltages (V_ref).
    #   V_in / ADC = ( 5*R_g*V_ref ) / (0x800000 * (R_g + 6000) )
    #
    # When R_g -> Inf reduces to
    #   V_in / ADC = ( 5*V_ref ) / 0x800000
    #
    # Short production history:
    #
    # Batches 1 and 2
    #   R_g = 11.8k +- 1%
    #   V_ref = 4.096 V
    #
    # Batch 3 (and future)
    #   R_g = Inf (omitted)
    #   V_ref = 2.048 V
    _serno_g1 = set(range(1, 41)) | set(range(101,121))
    _gain_g1 =  1.61846e-06 # V / ADC

    _serno_g2 = set(range(121, 141)) | set(range(201,206))
    _gain_g2 =  1.22070e-06 # V / ADC

    def __init__(self, chassis):
        self.chassis = chassis
        self.now = datetime.now()

## softioc bug. circa 4.6.1.  "soft" record with OMSL/DOL alreadys reports UDF_ALARM (does clear .UDF)
        for rec in (pv_samp_rate, pv_adc_sn):
            sevr = rec.get_field('SEVR')
            if sevr!='NO_ALARM':
                raise RuntimeError(f'Not ready {rec} : {sevr!r}')

        # Find acquisition rate in samples per second
        self.daq_rate = pv_samp_rate.get()
        self.serno = int(pv_adc_sn.get())

        _log.debug('Cal. S/N %d @ %d Hz', self.serno, self.daq_rate)

        if self.serno in self._serno_g1:
            _log.debug('Detected gain #1')
            self.expected_gain = self._gain_g1
        else:
            if self.serno in self._serno_g2:
                _log.debug('Detected gain #2')
            else:
                _log.warn('Unable to lookup expected gain.  Use #2')
            self.expected_gain = self._gain_g2

    def connect_dmm_afg(self):
        _log.debug('Connecting to DMM @%r', dmm_addr)
        self.dmm_sock = socket.create_connection(dmm_addr)
        self.dmm_rx = self.dmm_sock.makefile('rb', buffering=0)
        _log.debug('Connecting to AFG @%r', afg_addr)
        self.afg_sock = socket.create_connection(afg_addr)
        self.afg_rx = self.afg_sock.makefile('rb', buffering=0)

        # Grab DMM info
        self.dmm_sock.sendall(b'*IDN?\n')
        msg = self.dmm_rx.readline(1024).decode().strip()
        _log.debug('DMM info %r', msg)
        self.dmm_mfr, self.dmm_mdl, self.dmm_sn, self.dmm_fw = msg.split(',')[:4]

        # grab AFG info
        self.afg_sock.sendall(b'*IDN?\n')
        msg = self.afg_rx.readline(1024)
        _log.debug('DMM info %r', msg)

        # set DMM to longer integration period.  Default 10
        self.dmm_sock.sendall(b'VOLT:NPLC 100\n')

        # Zero AFG and set to high Z output
        self.set_afg(0.0)
        self.afg_sock.sendall(b'OUTPUT1:LOAD INF\n')

        pv_dmm_manu.set(self.dmm_mfr)
        pv_dmm_modl.set(self.dmm_mdl)
        pv_dmm_fw.set(self.dmm_fw)
        pv_dmm_sn.set(self.dmm_sn)

    def set_afg(self, v):
        self.afg_sock.sendall(f'APPL:DC DEF, DEF, {v:.1f}\n'.encode())
        # query AFG in lieu of an actual way to ensure the output has settled
        rbV = self._query_afg()
        assert abs(v-rbV)<0.001, (v, rbV)

    def _query_afg(self):
        self.afg_sock.sendall(b'APPL?\n')
        msg = self.afg_rx.readline(1024).strip().strip(b'"')
        # '"DC +1.0E+03,+2.000E-01,+0.00E+00"\n'
        assert msg.startswith(b'DC '), (type(msg), msg)
        parts = msg[3:].split(b',')
        return float(parts[2])

    def dmm_read(self, expect:float):
        assert expect!=0, "Don't expect zero..."
        self.dmm_sock.sendall(b'READ?\n')
        actual = float(self.dmm_rx.readline(1024).decode().strip())
        # sanity check absolute calibrations between AFG and DMM
        # eg. would catch impedance mis-match resulting in 2x difference
        err = (actual - expect)/expect
        assert numpy.abs(err)<0.01, err

        return actual

    # Wait for adc to settle at desired range
    def query_adc(self, expect: float):
        expect_counts = int(expect / self.expected_gain) # V / (V/ADC)

        # after an AFG setting change has settled, there may be
        # one acqusition in progress, and one being read out.
        # also skip the initial subscription update.
        # and one more for paranoia...
        nskip = 4

        updateQ = cothread.EventQueue(max_length=nskip*2)
        with ctxt.monitor(f'{args.prefix}{self.chassis:02d}:SA:V', updateQ.Signal):
            try:
                for _n in range(nskip):
                    updateQ.Wait(timeout=3) # wait for and discard initial update
            except Exception:
                raise RuntimeError('No Data.  IOC running?')

            try:
                grp = updateQ.Wait(timeout=15.0) # slowest update rate is 1k
            except Exception:
                raise RuntimeError('No Update.  Acquiring?')

        #tbase = grp['value.T']
        traces = numpy.stack(
            [grp.value[f'Ch{ch + 1:02d}'] for ch in range(num_adc_channels)],
            axis=1
        ) # [nsamp, 32]

        if traces.shape[0]<950: # need sufficent samples for stats
            raise RuntimeError('Insuf. samples. Incr. display period')

        stds = traces.std(axis=0)
        assert stds.shape==(32,), stds.shape

        assert stds.min() > 10, stds # impossibly low noise...

        # check that at least one channel has a sane reading
        # eg. catches AFG not connected to selected chassis

        if stds.min() > 400: # worst case in lab. setting is ~200 counts
            _log.error('excessive std: %r', stds)
            raise RuntimeError('Not settled')

        means = traces.mean(axis=0)
        merr = (means - expect_counts) / expect_counts

        if numpy.abs(merr).min() > 0.02:
            _log.error('abs. err exceed: %r', merr)
            raise RuntimeError('No signal?')

        return means

    def compute_calib(self, dmmPos, dmmNeg, adcPos, adcNeg):
        Y = numpy.asarray([dmmPos, dmmNeg])
        pv_dmm[0].set(Y[0])
        pv_dmm[1].set(Y[1])

        for ch, chan in enumerate(pv_chan):
            X = numpy.asarray([adcPos[ch], adcNeg[ch]])
            chan.NewADC1.set(int(X[0]))
            chan.NewADC2.set(int(X[1]))

            Yexpect = X / self.expected_gain

            slope, offset = numpy.polyfit(X, Y, 1) # linear fit
            _log.debug('ch %d fit X %r Y %r (%r) -> %f*x + %f', ch+1, X, Y, Yexpect, slope, offset)

            chan.NewOff.set(offset)
            chan.NewSlope.set(slope)

            chPass = True

            if abs(offset) > self.offset_tolerance:
                _log.info('Chan %d offset too large %r', ch+1, offset)
                chPass = False

            serr = (slope - self.expected_gain)/self.expected_gain

            if numpy.abs(serr) > self.slope_tolerance/100:
                _log.info('Chan %d slope out of range %r (%r)', ch+1, slope, serr)
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
        chan.NewOff.set(math.nan)

def update_chassis_sel():
    reset_status()
    chassis = pv_chassis_sel.get()
    _log.info('Chassis %d selected', chassis)

    pv_adc_sn.set_field('DOL', f'{args.prefix}{chassis:02d}:Quartz:serno CP MSS')

    # setup links to previous
    for i,chan in enumerate(pv_chan, 1):
        chan.PrevTCAL.set_field('DOL', f'{args.prefix}{chassis:02d}:SA:Ch{i:02d}:LASTCAL CP MSS')
        chan.PrevSlope.set_field('DOL', f'{args.prefix}{chassis:02d}:SA:Ch{i:02d}:ASLO CP MSS')
        chan.PrevOff.set_field('DOL', f'{args.prefix}{chassis:02d}:SA:Ch{i:02d}:AOFF CP MSS')

    pv_chassis_rb.set(chassis)

def run_calibration(chassis):
    reset_status()
    pv_status.set(1)
    try:
        _log.info('Chassis %d start calibration', chassis)

        for chan in pv_chan:
            chan.NewStatus.set(1) # Running

        report_status('Connect AFG/DMM')
        C = CalibProcess(chassis)
        C.connect_dmm_afg()

        report_status('Step positive')
        C.set_afg(9.0)
        adcPos = C.query_adc(9.0) # [32]
        dmmPos = C.dmm_read(9.0) # scalar

        report_status('Step negative')
        C.set_afg(-9.0)
        adcNeg = C.query_adc(-9.0) # [32]
        dmmNeg = C.dmm_read(-9.0) # scalar

        report_status('Computing')
        C.compute_calib(dmmPos, dmmNeg, adcPos, adcNeg)
        C.write_raw(dmmPos, dmmNeg, adcPos, adcNeg)
    except:
        _log.exception("calib fails")
        pv_status.set(2)
        raise
    else:
        pv_status.set(3)
        report_status('Success')
    finally:
        _log.debug('End Chassis %d start calibration', chassis)
        try:
            C.set_afg(0)
        except:
            _log.exception("Failed to zero AFG on completion")

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
            (f'{args.prefix}{chassis:02d}:SA:Ch{n:02d}:ASLO', slope),
            (f'{args.prefix}{chassis:02d}:SA:Ch{n:02d}:AOFF', offset),
            (f'{args.prefix}{chassis:02d}:SA:Ch{n:02d}:TCAL', tcal if status==3 else 0),
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
    parser.add_argument('-f', '--faddr',
                        default='192.168.83.48',
                        help='Specify function generator IP addr')
    parser.add_argument('-fp', '--fport',
                        type=int, default=5025,
                        help='Specify function generator SCPI port')
    parser.add_argument('-v', '--vaddr',
                        default='192.168.83.47',
                        help='Specify voltmeter IP addr')
    parser.add_argument('-vp', '--vport',
                        type=int, default=5025,
                        help='Specify voltmeter SCPI port')
    parser.add_argument('--prefix', default='FDAS:',
                        help='PV name prefix')
    return parser

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    args = getargs().parse_args()

    afg_addr = (args.faddr, args.fport)
    dmm_addr = (args.vaddr, args.vport)

    # Set up pvs
    builder.SetDeviceName(f'{args.prefix}Calib')

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
    pv_samp_rate = builder.longOut(
        'fsamp',
        OMSL='closed_loop',
        DOL=f'{args.prefix}ACQ:rate.RVAL CP MSS',
    )

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
            NewOff=builder.aIn(f'ch{chan:02d}:new:offset', PREC=3),
        ))

    # set up the ioc
    builder.LoadDatabase()
    softioc.iocInit()

    ctxt = Context(nt=False)  # PVA client

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
