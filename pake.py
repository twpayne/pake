#!/usr/bin/env python

import collections
import logging
from optparse import OptionParser
import os
import re
import subprocess
import time
import shutil
import sys


logger = logging.getLogger(__name__)


class PakeError(RuntimeError):
    pass


class AmbiguousRuleError(PakeError):

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return '%r matches multiple rules' % (self.name,)


class BuildError(PakeError):

    def __init__(self, target, message):
        self.target = target
        self.message = message

    def __str__(self):
        return '%s: %s' % (self.target.name, self.message)


class DuplicateTargetError(PakeError):

    def __init__(self, target):
        self.target = target

    def __str__(self):
        return 'duplicate target %r' % (self.target.name,)


class Target(object):

    def __init__(self, name, action=None, clean=True, dependencies=(),
                 makedirs=True, phony=False, precious=False):
        self.name = name
        self.action = action
        self._clean = clean
        self.dependencies = list(flatten(dependencies))
        self._makedirs = makedirs
        self.phony = phony
        self.precious = precious
        self.logger = logging.getLogger(self.name)
        self.timestamp = None

    def build(self, dry_run=False):
        timestamp = 0
        for dependency in self.dependencies:
            target = targets.get(dependency)
            timestamp = max(timestamp, target.build(dry_run=dry_run))
        self.debug('build')
        if self.timestamp is None:
            if not self.phony and os.path.exists(self.name):
                self.timestamp = os.stat(self.name).st_mtime
            else:
                self.timestamp = -1
        if self.timestamp < timestamp:
            self.debug('action')
            if self._makedirs and not dry_run:
                self.makedirs(os.path.dirname(self.name))
            if self.action:
                if self.action.__doc__:
                    self.info(self.action.__doc__)
                if not dry_run:
                    self.action(self)
            self.timestamp = timestamp or time.time()
        return self.timestamp

    def cp(self, *args):
        dest = args.pop()
        for arg in args:
            shutil.copy(arg, dest)

    def clean(self, really=False, recurse=True):
        if (self._clean or really) and not self.precious:
            self.info('clean')
            try:
                os.remove(self.name)
            except OSError:
                pass
        if recurse:
            for dependency in self.dependencies:
                targets.get(dependency).clean(really=really, recurse=recurse)

    def debug(self, *args, **kwargs):
        self.logger.debug(*args, **kwargs)

    def error(self, message):
        raise BuildError(self, message)

    def info(self, *args, **kwargs):
        self.logger.info(*args, **kwargs)

    def makedirs(self, path):
        if path and not os.path.exists(path):
            os.makedirs(path)

    def output(self, *args, **kwargs):
        args = list(flatten(args))
        self.info(' '.join(args))
        try:
            output = subprocess.check_output(args, **kwargs)
            with open(self.name, 'w') as f:
                f.write(output)
        except subprocess.CalledProcessError as e:
            self.clean(recurse=False)
            self.error(e)

    def run(self, *args, **kwargs):
        args = list(flatten(args))
        self.info(' '.join(args))
        try:
            subprocess.check_call(args, **kwargs)
        except subprocess.CalledProcessError as e:
            self.clean(recurse=False)
            self.error(e)

    def touch(self):
        if os.path.exists(self.name):
            os.utime(self.name, None)
        else:
            with open(self.name, 'w'):
                pass


class TargetCollection(object):

    def __init__(self):
        self.default = None
        self.targets = {}

    def add(self, target):
        if target.name in self.targets:
            raise DuplicateTargetError(target)
        self.targets[target.name] = target
        if self.default is None:
            self.default = target

    def get(self, name):
        if name in self.targets:
            return self.targets[name]
        target = None
        for regexp, f in rules.iteritems():
            match = regexp.search(name)
            if not match:
                continue
            if target is not None:
                raise AmbiguousRuleError(name)
            target = f(name, match)
        if target is None:
            target = Target(name, precious=True)
        self.targets[name] = target
        return target


class VariableCollection(dict):

    def __init__(self, **kwargs):
        dict.__init__(self)
        self.update(kwargs)

    def __setitem__(self, key, value):
        if key not in self:
            dict.__setitem__(self, key, value)


targets = TargetCollection()
rules = {}
variables = VariableCollection(**os.environ)


def flatten(*args):
    for arg in args:
        if (isinstance(arg, collections.Iterable) and
                not isinstance(arg, basestring)):
            for element in flatten(*arg):
                yield element
        else:
            yield arg


def ifind(*paths):
    for path in paths:
        for dirpath, dirnames, names in os.walk(path):
            for name in names:
                yield os.path.join(dirpath, name)


def main(argv=sys.argv):
    option_parser = OptionParser()
    option_parser.add_option('-c', '--clean',
                             action='store_true')
    option_parser.add_option('-n', '--dry-run', '--just-print', '--recon',
                             action='store_true')
    option_parser.add_option('-r', '--really',
                             action='store_true')
    option_parser.add_option('-v', '--verbose',
                             action='count', dest='logging_level')
    option_parser.set_defaults(logging_level=0)
    options, args = option_parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.INFO - 10 * options.logging_level)
    targets_ = []
    for arg in args:
        match = re.match(r'(?P<key>\w+)=(?P<value>.*)\Z', arg)
        if match:
            key, value = match.group('key', 'value')
            if not key in variables:
                logger.error('%s is not a variable', key)
            logger.debug('%s=%r', key, value)
            dict.__setitem__(variables, key, value)
            continue
        targets_.append(arg)
    if not targets_:
        targets_ = (targets.default.name,)
    try:
        for target in targets_:
            target = targets.get(target)
            if options.clean:
                target.clean(really=options.really, recurse=True)
            else:
                target.build(dry_run=options.dry_run)
    except BuildError as e:
        logger.error(e)
        sys.exit(1)


def output(*args):
    args = list(flatten(args))
    logger.debug(' '.join(args))
    return subprocess.check_output(args)


def rule(pattern):
    def f(targetmaker):
        rules[re.compile(pattern)] = targetmaker
    return f


def target(name, *dependencies, **kwargs):
    def f(action):
        target = Target(name, action=action, dependencies=dependencies,
                        **kwargs)
        targets.add(target)
    return f


def virtual(name, *dependencies, **kwargs):
    target = Target(name, dependencies=dependencies, clean=False, phony=True,
                    **kwargs)
    targets.add(target)
