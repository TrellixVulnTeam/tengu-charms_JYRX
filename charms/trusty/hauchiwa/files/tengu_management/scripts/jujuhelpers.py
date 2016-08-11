# Copyright (C) 2016  Ghent University
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#pylint: disable=r0201,c0111,c0325,c0301
#
""" Handles communication to Juju """

from subprocess import CalledProcessError, Popen, PIPE, check_call
from time import sleep
import json
import sys
from base64 import b64encode, b64decode
import getpass
import tempfile
import os

# non standard pip dependencies
import yaml

from output import fail


USER = getpass.getuser()
HOME = '/home/{}'.format(USER)


class JujuException(Exception):
    pass

class JujuNotFoundException(JujuException):
    pass


class Service(object):
    def __init__(self, name, env):
        self.env = env
        self.name = name

    def upgrade(self):
        self.env.do('upgrade-charm', self.name)

    @property
    def exists(self):
        try:
            return self.status is not None
        except JujuNotFoundException:
            return False

    @property
    def status(self):
        """ Return status of service can be either None or following dict:
        {
            'service-status': '...',
            'message': '...',
        }"""
        info = self.env.status['services'].get(self.name)
        if not info:
            raise JujuNotFoundException('service {} not found'.format(self.name))
        return info

    def get_config(self):
        """ Return dictionary with config of service"""
        config = self.config
        export = {}
        for name, option in config['settings'].iteritems():
            if option.get('value'):
                export[name] = option['value']
        return export


    @property
    def config(self):
        """ Return dictionary with output of 'juju get <servicename>'"""
        output = self.env.do('get', self.name, format='yaml')
        return yaml.load(output)

    def set_config(self, config):
        """ Update config of service to values in given dictionary. Format of dictionary:
        {
            '<config-key>': '....',
        }

        Please note that this format is different from what you get when asking the config of a service."""
        config = {self.name: config}
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                # do stuff with temp file
                tmp.write(yaml.dump(config))
            self.env.do('set', self.name, config=tmp.name)
        finally:
            os.remove(tmp.name)

    def wait_until(self, requested_status):
        """ Wait until service contains status in its message """
        sys.stdout.write('waiting until {} service is {} '.format(self.name, requested_status))
        while(True):
            current_status = self.status
            if (current_status['service-status']['current'] and current_status['service-status'].get('message') and (requested_status.lower() in current_status['service-status'].get('message').lower())):
                break
            sleep(5)
            sys.stdout.write('.')
            sys.stdout.flush()
        sys.stdout.write('{} is {}!\n'.format(self.name, requested_status))


    def add_unit(self, **options):
        """ Add unit to existing Charm"""
        self.env.do('add-unit', self.name, **options)

    def destroy(self, force=False):
        machines_to_destroy = [u['machine'] for u in self.status['units'].values()]
        self.env.do('destroy-service', self.name)
        if force:
            for machinename in machines_to_destroy:
                if not any(x in machinename for x in ['/lxc/', 'kvm/']): # only destroy containers
                    print("not destroying {} because it is a physical machine".format(machinename))
                    machines_to_destroy.remove(machinename)
            machine_unit_map = self.env.machine_unit_map
            import re
            pattern = re.compile("^{}/[0-9]*".format(self.name))
            for machinename in machines_to_destroy:
                for unit in machine_unit_map[machinename]:
                    if not pattern.match(unit): # Remove machines if unit from other service is still using machine
                        machines_to_destroy.remove(machinename)
                        break
            print('destroying machines {}'.format(machines_to_destroy))
            for machinename in machines_to_destroy:
                self.env.do('destroy-machine', machinename, '--force')


class JujuEnvironment(object):
    """ handles an existing Juju environment """
    def __init__(self, _name=None):
        if _name:
            self.name = _name
        else:
            self.name = self.current_env()

    @property
    def machines(self):
        """ Return machines"""
        return self.status['machines'].keys()

    @property
    def machine_unit_map(self):
        """ Return dictionary with keys = machines, value = list of units deployed on that machine"""
        mu_map = {}
        status = self.status
        for service in status['services'].values():
            for unitname, unit in service['units'].items():
                mu_map.setdefault(unit['machine'], []).append(unitname)
        return mu_map


    @property
    def services(self):
        """ Returns services"""
        return self.status['services'].keys()

    @property
    def status(self):
        """ Return dictionary with output of juju status """
        output = self.do('status', format='json')
        return json.loads(output)

    @property
    def password(self):
        """ Gets the default password from the Juju environment"""
        file_path = '{}/.juju/environments/{}.jenv'.format(HOME, self.name)
        stream = open(file_path, "r")
        doc = yaml.load(stream)
        password = doc.get('password')
        return password

    @property
    def bootstrap_user(self):
        """ Gets the bootstrap user from the Juju environment"""
        file_path = '{}/.juju/environments/{}.jenv'.format(HOME, self.name)
        stream = open(file_path, "r")
        doc = yaml.load(stream)
        password = doc.get('bootstrap-config').get('bootstrap-user')
        return password


    def set_active(self):
        """switch active juju environment to self"""
        JujuEnvironment.juju_do_call('switch', self.name)

    def add_machines(self, machines):
        """ Add all machines received from provider to Juju environment"""
        print "adding machines to juju"
        processes = set()
        for machinefqdn in machines:
            print '\tAdding %s' % machinefqdn
            processes.add(Popen([
                'juju', 'add-machine',
                'ssh:{}@{}'.format(self.bootstrap_user, machinefqdn)
            ]))
            sleep(10)
        for proc in processes:
            if proc.poll() is None:
                proc.wait()
            if proc.poll() > 0:
                raise JujuException('error while adding machines')


    def deploy(self, charm, name, **options):
        """ Deploy <charm> as <name> with config in <config_path> """
        self.do_call('deploy', charm, name, **options)
        return Service(name, self)

    def deploy_bundle(self, bundle_path, *args, **options):
        """ Deploy Juju bundle """
        return self.do_call('deployer', '-c', bundle_path, *args, **options)

    def add_relation(self, charm1, charm2):
        """ add relation between two charms """
        self.do_call('add-relation', charm1, charm2)

    def action_do(self, unit, action, **options):
        return self.do('action do', unit, action, **options)

    def do(self, action, *args, **kwargs): #pylint: disable=c0103
        args += ('-e', self.name)
        return JujuEnvironment.juju_do(action, *args, **kwargs)

    def do_call(self, action, *args, **kwargs): #pylint: disable=c0103
        args += ('-e', self.name)
        JujuEnvironment.juju_do_call(action, *args, **kwargs)

    #
    # Tengu specific methods
    #

    def deploy_init_bundle(self, bundle_path):
        with open(bundle_path, 'r') as stream:
            bundle = yaml.load(stream.read())
        services = bundle['services']

        def custom_sorting_key(service):
            return (service[1]['annotations']['order'])
        services = sorted(services.items(), key=custom_sorting_key)

        for name, service in services:
            # Put config in temp file so we can give it to the charm deploy command.
            # Leave config empty when no config in bundle.
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(yaml.dump({name: service.get('options', dict())}))
            tmp.close()
            # Deploy to all machines if "to" not specified
            to_machines = service.get('to', self.machines)
            deployed_service = self.deploy(service['charm'], name, to=to_machines.pop(), config=tmp.name)
            os.remove(tmp.name) # No finally because we don't want to delete the file if deploying failed.
            for machine in to_machines:
                deployed_service.add_unit(to=machine)
            # Wait until status message "Ready" if no status message is specified
            deployed_service.wait_until(service['annotations'].get('wait-until-message', 'Ready'))


    def return_environment(self):
        """ returns exported juju environment"""
        env_conf = {'environment-name': str(self.name)}
        with open('{}/.juju/environments.yaml'.format(HOME), 'r') as e_file:
            e_content = yaml.load(e_file)
        env_conf['environment-config'] = b64encode(
            yaml.dump(
                e_content['environments'][self.name],
                default_flow_style=False
            )
        )
        with open('{}/.juju/environments/{}.jenv'.format(HOME, self.name),
                  'r') as e_file:
            e_content = e_file.read()
        env_conf['environment-jenv'] = b64encode(e_content)
        return env_conf

    def destroy_containers(self):
        """force destroy containers"""
        for machine in self.status['machines'].values():
            if machine.get('containers'):
                for container in machine['containers'].keys():
                    self.do_call('destroy-machine', container, '--force')

    #
    # Static methods
    #

    @staticmethod
    def juju_do(action, *args, **kwargs):
        """Juju sometimes spits warnings to stderr that could fuck up parsing of
        the output. Use this function when you want to parse Juju output. Throws
        exception when returncode is not 0. Exception also includes stderr"""
        command = ['juju', action]
        # Add all the arguments to the command
        command.extend(args)
        # Ad all the keyword arguments to the command
        for key, value in kwargs.iteritems():
            command.extend(['--{}'.format(key), value])
        # convert all elements in command to string
        command = [str(i) for i in command]
        try:
            output = check_stdout(command)
            return output
        except CalledProcessError as ex:
            if 'missing namespace, config not prepared' in ex.output:
                raise JujuNotFoundException("missing namespace, config not prepared")
            if "ERROR Unable to connect to environment" in ex.output:
                raise JujuNotFoundException("ERROR Unable to connect to environment")
            raise JujuException("{}\nCOMMAND: {}".format(ex.output, " ".join(command)))

    @staticmethod
    def juju_do_call(action, *args, **kwargs):
        """Use this function when you want Juju to do something but you don't
        need to parse the output."""
        command = ['juju', action]
        # Add all the arguments to the command
        command.extend(args)
        # Ad all the keyword arguments to the command
        for key, value in kwargs.iteritems():
            command.extend(['--{}'.format(key), value])
        # convert all elements in command to string
        command = [str(i) for i in command]
        try:
            check_call(command)
        except CalledProcessError as ex:
            if 'missing namespace, config not prepared' in ex.output:
                raise JujuNotFoundException("missing namespace, config not prepared")
            if "ERROR Unable to connect to environment" in ex.output:
                raise JujuNotFoundException("ERROR Unable to connect to environment")
            raise JujuException("{}\nCOMMAND: {}".format(ex.output, " ".join(command)))

    @staticmethod
    def current_env():
        """ Returns the current active Juju environment """
        return JujuEnvironment.juju_do('switch').rstrip()

    @staticmethod
    def list_environments():
        """Checks if Juju env with given name exists."""
        return JujuEnvironment.juju_do('switch', '--list').split()

    @staticmethod
    def env_exists(name):
        """Checks if Juju env with given name exists."""
        envs = JujuEnvironment.list_environments()
        return name in envs

    @staticmethod
    def create(name, juju_config, machines, bundle):
        """Creates Juju environment and deploy the init bundle."""
        if JujuEnvironment.env_exists(name):
            fail("Juju environment already exists. Remove it first with 'tengu destroy-model {}'".format(name))
        JujuEnvironment._create_env(name, juju_config)
        # Wait 5 seconds before adding machines because python
        # is too fast for juju
        sleep(20)
        environment = JujuEnvironment(name)
        environment.add_machines(machines)
        environment.deploy_init_bundle(bundle)
        return environment


    @staticmethod
    def _create_env(name, juju_config):
        """ Add new Juju environment and bootstrap it """
        print "adding juju environment %s" % name
        name = str(name)
        # get original environments config
        with open("{}/.juju/environments.yaml".format(HOME), 'r') as config_file:
            config = yaml.load(config_file)
        if config['environments'] is None:
            config['environments'] = dict()
        # add new environment
        config['environments'][name] = juju_config
        # write new environments config
        with open("{}/.juju/environments.yaml".format(HOME), 'w') as config_file:
            config_file.write(yaml.dump(config, default_flow_style=False))
        # Bootstrap new environmnent
        try:
            env = JujuEnvironment(name)
            env.set_active()
            print "bootstrapping juju environment"
            sleep(5) # otherwise we get a weird error
            env.do_call('bootstrap', "--debug")
        except CalledProcessError as ex:
            raise JujuException(ex.output)


    @staticmethod
    def import_environment(env_conf):
        name = env_conf['environment-name']
        conf = yaml.load(b64decode(env_conf['environment-config']))
        jenv = b64decode(env_conf['environment-jenv'])
        with open('{}/.juju/environments.yaml'.format(HOME), 'r') as e_file:
            e_content = yaml.load(e_file)
        with open('{}/.juju/environments.yaml'.format(HOME), 'w+') as e_file:
            e_content['environments'][name] = conf
            e_file.write(yaml.dump(e_content, default_flow_style=False))
        with open('{}/.juju/environments/{}.jenv'.format(HOME, name), 'w+') as e_file:
            e_file.write(jenv)
        env = JujuEnvironment(name)
        env.set_active()


def check_stdout(*popenargs, **kwargs):
    """Return stdout. Throw error that includes stderr."""
    process = Popen(stdout=PIPE, stderr=PIPE, *popenargs, **kwargs)
    stdout, stderr = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise CalledProcessError(retcode, cmd, output=stdout + stderr)
    return stdout
