# fedup.upgrade - actually run the upgrade.
# For the sake of simplicity, we don't bother with yum here.

import rpm
from rpm._rpm import ts as TransactionSetCore

import os, tempfile
from threading import Thread

import logging
log = logging.getLogger('fedup.upgrade')

from fedup import _

class TransactionSet(TransactionSetCore):
    flags = TransactionSetCore._flags
    vsflags = TransactionSetCore._vsflags
    color = TransactionSetCore._color

    def run(self, callback, data, probfilter):
        log.debug('ts.run()')
        rv = TransactionSetCore.run(self, callback, data, probfilter)
        problems = self.problems()
        if rv != rpm.RPMRC_OK and problems:
            raise TransactionError(problems)
        return rv

    def check(self, *args, **kwargs):
        TransactionSetCore.check(self, *args, **kwargs)
        # NOTE: rpm.TransactionSet throws out all problems but these
        return [p for p in self.problems()
                  if p.type in (rpm.RPMPROB_CONFLICT, rpm.RPMPROB_REQUIRES)]

    def add_install(self, path, key=None, upgrade=False):
        log.debug('add_install(%s, %s, upgrade=%s)', path, key, upgrade)
        if key is None:
            key = path
        retval, header = self.hdrFromFdno(open(path))
        if retval != rpm.RPMRC_OK:
            raise rpm.error("error reading package header")
        if not self.addInstall(header, key, upgrade):
            raise rpm.error("adding package to transaction failed")

    def __del__(self):
        self.closeDB()

probtypes = { rpm.RPMPROB_NEW_FILE_CONFLICT : _('file conflicts'),
              rpm.RPMPROB_FILE_CONFLICT : _('file conflicts'),
              rpm.RPMPROB_OLDPACKAGE: _('older package(s)'),
              rpm.RPMPROB_DISKSPACE: _('insufficient disk space'),
              rpm.RPMPROB_DISKNODES: _('insufficient disk inodes'),
              rpm.RPMPROB_CONFLICT: _('package conflicts'),
              rpm.RPMPROB_PKG_INSTALLED: _('package already installed'),
              rpm.RPMPROB_REQUIRES: _('required package'),
              rpm.RPMPROB_BADARCH: _('package for incorrect arch'),
              rpm.RPMPROB_BADOS: _('package for incorrect os'),
            }

class FedupError(Exception):
    pass

class TransactionError(FedupError):
    def __init__(self, problems):
        self.problems = problems

def pipelogger(pipe, level=logging.INFO):
    logger = logging.getLogger("fedup.rpm")
    logger.info("opening pipe")
    with open(pipe, 'r') as fd:
        for line in fd:
            if line.startswith('D: '):
                logger.debug(line[3:].rstrip())
            else:
                logger.log(thislevel, line.rstrip())
        logger.info("got EOF")
    logger.info("exiting")

logging_to_rpm = {
    logging.DEBUG:      rpm.RPMLOG_DEBUG,
    logging.INFO:       rpm.RPMLOG_INFO,
    logging.WARNING:    rpm.RPMLOG_WARNING,
    logging.ERROR:      rpm.RPMLOG_ERR,
    logging.CRITICAL:   rpm.RPMLOG_CRIT,
}

class FedupUpgrade(object):
    def __init__(self, root='/', logpipe=True, rpmloglevel=logging.INFO):
        self.root = root
        self.ts = None
        self.logpipe = None
        rpm.setVerbosity(logging_to_rpm[rpmloglevel])
        if logpipe:
            self.logpipe = self.openpipe()

    def setup_transaction(self, pkgfiles, check_fatal=False):
        log.debug("starting")
        # initialize a transaction set
        self.ts = TransactionSet(self.root, rpm._RPMVSF_NOSIGNATURES)
        if self.logpipe:
            self.ts.scriptFd = self.logpipe.fileno()
        # populate the transaction set
        for pkg in pkgfiles:
            try:
                self.ts.add_install(pkg, upgrade=True)
            except rpm.error as e:
                log.warn('error adding pkg: %s', e)
                # TODO: error callback
        log.debug('ts.check()')
        problems = self.ts.check()
        if problems:
            log.info("problems with transaction check:")
            for p in problems:
                log.info(p)
            if check_fatal:
                raise TransactionError(problems=problems)
        log.debug('ts.order()')
        self.ts.order()
        log.debug('ts.clean()')
        self.ts.clean()
        log.debug('transaction is ready')

    def openpipe(self):
        log.debug("creating log pipe")
        pipefile = tempfile.mktemp(prefix='fedup-rpm-log.')
        os.mkfifo(pipefile, 0600)
        log.debug("starting logging thread")
        pipethread = Thread(target=pipelogger, name='pipelogger',
                                 args=(pipefile,))
        pipethread.daemon = True
        pipethread.start()
        log.debug("opening log pipe")
        pipe = open(pipefile, 'w')
        rpm.setLogFile(pipe)
        return pipe

    def closepipe(self):
        log.debug("closing log pipe")
        rpm.setVerbosity(rpm.RPMLOG_WARNING)
        rpm.setLogFile(None)
        if self.ts:
            self.ts.scriptFd = None
        self.logpipe.close()
        os.remove(self.logpipe.name)
        self.logpipe = None

    def run_transaction(self, callback):
        assert callable(callback.callback)
        probfilter = ~rpm.RPMPROB_FILTER_DISKSPACE
        rv = self.ts.run(callback.callback, None, probfilter)
        if rv != 0:
            log.info("ts completed with problems - code %u", rv)
        return rv

    def test_transaction(self, callback):
        self.ts.flags = rpm.RPMTRANS_FLAG_TEST
        try:
            return self.run_transaction(callback)
        finally:
            self.ts.flags &= ~rpm.RPMTRANS_FLAG_TEST

    def __del__(self):
        if self.logpipe:
            self.closepipe()
