"""Exec the existing hardware encoder in an explicitly parked-only low-rate profile."""
import os
from server.sentry_common import enabled, config

def main():
    if not enabled(resource='encoder'):
        return
    cfg=config()
    env=os.environ.copy()
    for key in ('width','height','fps','bitrate'):
        env['NAP_SENTRY_'+key.upper()]=str(cfg[key])
    binary='/data/openpilot/system/loggerd/encoderd'
    os.execve(binary,[binary,'--sentry'],env)

if __name__=='__main__':main()
