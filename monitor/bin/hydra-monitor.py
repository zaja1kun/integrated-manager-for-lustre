#!/usr/bin/env python

from django.core.management import setup_environ
import settings
setup_environ(settings)

from monitor.lib.lustre_audit import LustreAudit

from time import sleep
import os
import sys

if __name__=='__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--once":
            LustreAudit().audit_all()
        else:
            while(True):
                LustreAudit().audit_all()
                sleep(5)

    except KeyboardInterrupt:
        print "Exiting..."
        os._exit(0)

