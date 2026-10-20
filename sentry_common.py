"""Shared Sentry V2 leases, conservative settings, and bounded evidence storage."""
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

LEASE = Path('/dev/shm/nap_sentry.json')
RECORD_LEASE = Path('/dev/shm/nap_sentry_record.json')
WORKER_STATUS = Path('/dev/shm/nap_sentry_worker.json')
ROOT = Path('/data/nap_sentry/events')
CONFIG = Path('/data/nap_sentry/config.json')
DEFAULTS = dict(analysis_fps=2, pixel_threshold=18, changed_fraction=.035, confirmation_frames=3,
                brightness_jump=25, clip_seconds=30, max_clip_seconds=120, cooldown_seconds=15,
                storage_mb=512, reserve_mb=1024, warn_c=55, suspend_c=60, armed_minutes=30,
                width=640, height=360, fps=10, bitrate=500000)
LIMITS = dict(analysis_fps=(1,2), pixel_threshold=(8,80), changed_fraction=(.005,.5), confirmation_frames=(2,6),
              brightness_jump=(10,60), clip_seconds=(10,60), max_clip_seconds=(30,180), cooldown_seconds=(5,120),
              storage_mb=(64,2048), reserve_mb=(512,4096), warn_c=(45,60), suspend_c=(50,65), armed_minutes=(5,120),
              width=(320,1280), height=(240,720), fps=(5,15), bitrate=(200000,2000000))

def validate_config(changes, base=None):
    out = dict(DEFAULTS if base is None else base)
    if not isinstance(changes, dict) or any(k not in LIMITS for k in changes):
        raise ValueError('Unknown Sentry setting')
    for key, value in changes.items():
        lo, hi = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError('Invalid ' + key)
        if isinstance(DEFAULTS[key], int) and int(value) != value:
            raise ValueError('Integer required for ' + key)
        out[key] = type(DEFAULTS[key])(value)
    if out['warn_c'] >= out['suspend_c'] or out['max_clip_seconds'] < out['clip_seconds']:
        raise ValueError('Invalid thermal or recording limits')
    if out['width'] % 2 or out['height'] % 2:
        raise ValueError('Encoder dimensions must be even')
    return out

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    tmp.write_text(json.dumps(data,allow_nan=False))
    tmp.replace(path)

def read(path, default=None):
    try:
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else ({} if default is None else default)
    except (OSError, ValueError):
        return {} if default is None else default

def config():
    try:
        return validate_config(read(CONFIG))
    except (ValueError,TypeError):
        return dict(DEFAULTS)

def lease():
    d = read(LEASE)
    now = time.monotonic()
    try:
        if now < d['until'] <= now + 4 and now < d['deadline'] <= now + 7201:
            return d
    except (KeyError, TypeError):
        pass
    return {}

def enabled(started=False, resource='camera'):
    if started:
        return False
    d = lease()
    if not d:
        return False
    if resource == 'live':
        return bool(d.get('live', False))
    if resource == 'encoder':
        r = read(RECORD_LEASE)
        until=r.get('until',0)
        return bool(d.get('live', False) or (isinstance(until,(int,float)) and time.monotonic() < until <= time.monotonic()+4))
    return True

class Store:
    def __init__(self, root=None):
        self.root = ROOT if root is None else Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
    @contextlib.contextmanager
    def locked(self):
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
    def path(self, ident, suffix):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):
            raise ValueError('Invalid event ID')
        p = self.root / (ident + suffix)
        if p.is_symlink() or p.resolve().parent != self.root.resolve():
            raise ValueError('Invalid evidence path')
        return p
    def events(self):
        rows=[]
        for p in self.root.glob('*.json'):
            if p.is_symlink():
                continue
            d=read(p)
            if d.get('id') == p.stem and re.fullmatch(r'[0-9a-f]{32}',p.stem):
                rows.append(d)
        return sorted(rows,key=lambda x:x.get('timestamp',0),reverse=True)
    def usage(self):
        used=sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and not p.is_symlink())
        return {'used_bytes':used,'free_bytes':shutil.disk_usage(self.root).free,'event_count':len(self.events())}
    def prune(self, cfg):
        with self.locked():
            usage=self.usage()
            for d in reversed(self.events()):
                if usage['used_bytes'] < cfg['storage_mb']*1048576 and usage['free_bytes'] > cfg['reserve_mb']*1048576 and usage['event_count'] < 1000:
                    break
                if not d.get('protected') and d.get('state') not in ('recording','finalizing'):
                    self._delete(d['id']);usage=self.usage()
            return usage['used_bytes'] < cfg['storage_mb']*1048576 and usage['free_bytes'] > cfg['reserve_mb']*1048576 and usage['event_count'] < 1000
    def _delete(self, ident):
        for suffix in ('.json','.jpg','.mp4','.partial.h264','.partial.mp4'):
            self.path(ident,suffix).unlink(missing_ok=True)
    def delete(self, ident):
        with self.locked():
            d=read(self.path(ident,'.json'))
            if d.get('state') in ('recording','finalizing'):
                raise ValueError('Stop recording before deleting')
            self._delete(ident)
    def metadata(self,d):
        atomic(self.path(d['id'],'.json'),d)
    def create(self, kind, thumbnail, cfg):
        if not self.prune(cfg):
            raise ValueError('Security storage allowance or free-space reserve reached')
        ident=uuid.uuid4().hex
        d={'id':ident,'timestamp':time.time(),'kind':kind,'state':'snapshot' if kind=='snapshot' else 'recording','protected':False,'camera':'road','bytes':0}
        if thumbnail:
            self.path(ident,'.jpg').write_bytes(thumbnail)
        self.metadata(d)
        return d
    def finalize(self,d,fps,interrupted=False):
        raw=self.path(d['id'],'.partial.h264');part=self.path(d['id'],'.partial.mp4')
        d.update(state='finalizing',interrupted=interrupted)
        self.metadata(d)
        try:
            if not raw.exists() or raw.stat().st_size == 0:
                raise ValueError('No encoded frames received')
            ffmpeg='/data/server/ffmpeg_full'
            subprocess.run([ffmpeg,'-v','error','-nostdin','-y','-fflags','+genpts','-r',str(fps),'-i',str(raw),'-c:v','copy','-movflags','+faststart',str(part)],check=True,timeout=3,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            part.replace(self.path(d['id'],'.mp4'))
            d.update(state='ready',bytes=self.path(d['id'],'.mp4').stat().st_size)
        except Exception as e:
            d.update(state='failed',error=str(e)[:160])
        finally:
            raw.unlink(missing_ok=True);part.unlink(missing_ok=True)
            self.metadata(d)
    def recover(self, cfg):
        for d in self.events():
            if d.get('state') in ('recording','finalizing'):
                self.finalize(d,d.get('fps',cfg['fps']),True)
        # Only Sentry-owned partial files inside this dedicated directory.
        for p in self.root.glob('*.partial.*'):
            if not p.is_symlink():
                p.unlink(missing_ok=True)
        self.prune(cfg)
