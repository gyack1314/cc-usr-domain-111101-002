from decimal import Decimal
def split_interval(lo,hi,points):
    if lo>=hi:raise ValueError('区间须有正长度')
    cuts=[lo]+sorted({x for x in points if lo<x<hi})+[hi]
    return list(zip(cuts,cuts[1:]))
def prorate(meter_lo,meter_hi,time_lo,time_hi,lo,hi):
    if time_lo>=time_hi or not time_lo<=lo<=hi<=time_hi:raise ValueError('切分越界')
    return (Decimal(str(meter_hi))-Decimal(str(meter_lo)))*Decimal(str(hi-lo))/Decimal(str(time_hi-time_lo))
def edge_issues(a,b,max_gap,max_speed):
    issues=[];dt=b['at']-a['at'];distance=Decimal(b['meter'])-Decimal(a['meter'])
    if a['epoch']!=b['epoch']:issues.append('EPOCH_CHANGE')
    if dt<=0 or dt>max_gap:issues.append('TIME_GAP')
    if distance<0:issues.append('COUNTER_REVERSE')
    if dt>0 and distance/Decimal(str(dt))>Decimal(str(max_speed)):issues.append('SPEED_OUTLIER')
    return issues
def time_seconds(value):
    from datetime import datetime,timezone
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('时间必须包含偏移')
    delta=stamp.astimezone(timezone.utc)-datetime(1970,1,1,tzinfo=timezone.utc)
    return Decimal(delta.days*86400+delta.seconds)+Decimal(delta.microseconds)/1000000
