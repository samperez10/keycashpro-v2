"""Global pacing ranges; each worker draws its own independent timings."""
import math
import random
import time

DEFAULT_PACING={'solve_delay_min':25,'solve_delay_max':40,'rests_enabled':True,
                'work_min_minutes':15,'work_max_minutes':25,
                'rest_min_minutes':3,'rest_max_minutes':7}


def validate_pacing(settings):
    if type(settings['rests_enabled']) is not bool:raise ValueError('Timed rests must be enabled or disabled')
    for lo,hi,label,minimum,maximum in (
        ('solve_delay_min','solve_delay_max','Solve delay',0,3600),
        ('work_min_minutes','work_max_minutes','Work session',1,1440),
        ('rest_min_minutes','rest_max_minutes','Rest duration',.1,1440)):
        for key in (lo,hi):
            try:value=float(settings[key])
            except (ValueError,TypeError):raise ValueError(label+': enter a number') from None
            if not math.isfinite(value) or not minimum<=value<=maximum:raise ValueError(f'{label}: use {minimum}–{maximum}')
            settings[key]=value
        if settings[lo]>settings[hi]:raise ValueError(label+': minimum cannot exceed maximum')


class Pacer:
    def __init__(self,settings,*,clock=time.monotonic,rng=None):
        self.settings=settings;self.clock=clock;self.rng=rng or random.Random()
        self.work_until=None;self.rests_completed=0
    def delay(self):
        s=self.settings();return self.rng.uniform(s['solve_delay_min'],s['solve_delay_max'])
    def begin_work(self):
        s=self.settings()
        self.work_until=self.clock()+60*self.rng.uniform(s['work_min_minutes'],s['work_max_minutes']) if s['rests_enabled'] else None
    def rest_due(self):
        if not self.settings()['rests_enabled']:self.work_until=None;return False
        if self.work_until is None:self.begin_work()
        return self.clock()>=self.work_until
    def rest_seconds(self):
        s=self.settings();return 60*self.rng.uniform(s['rest_min_minutes'],s['rest_max_minutes'])
    def pause_work_clock(self,elapsed):
        if self.work_until is not None:self.work_until+=max(0,elapsed)
