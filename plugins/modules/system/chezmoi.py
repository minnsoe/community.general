#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2021, Minn Soe <contributions@minn.io>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: chezmoi

short_description: Manages user dotfiles with chezmoi.

version_added: "0.1.0"

description: Module for managing dotfiles in a user's home directory with chezmoi.

options:
    repo:
        description:
            - URL to a specific dotfiles repository, e.g. U(https://github.com/minnsoe/dotfiles).
            - Pattern (user) on GitHub or Sourcehut with a repository named dotfiles.
            - Pattern (user/repo) on GitHub or Sourcehut.
        required: false
        type: str
    state:
        description:
            - Latest ensures dotfiles are updated against the VCS and applied.
            - Present ensures dotfiles are applied.
            - Absent removes tracked dotfiles provided the state has not been purged with I(purged=true).
        required: true
        type: str
        choices: [ present, absent, latest ]
    config:
        description:
            - Chezmoi config and data for task invocation in yaml format U(https://www.chezmoi.io/docs/reference/#configuration-file).
            - This file is removed after invocation, consider using a C(.chezmoi.<format>.tmpl) template file if you want to persist the config.
            - Useful for setting non-interactive template values and prevent hanging with prompts.
        required: false
        type: dict
    purge:
        description:
            - Removes chezmoi source directory, state and configuration. Leaves target dotfiles intact.
        required: false
        type: bool
        default: no
    source:
        description:
            - Uses provided directory as the source instead of C(${HOME}/.local/share/chezmoi).
        required: false
        type: path
    dest:
        description:
            - Uses provided directory as the destination instead of C(${HOME}).
        required: false
        type: path
    path:
        description:
            - Path in which to search for the C(chezmoi) binary.
        required: false
        type: path
        default: /usr/local/bin/

author:
    - Minn Soe (@minnsoe)
'''

EXAMPLES = r'''
# Basic usage
- name: Ensure user dotfiles are up-to-date against repo
  community.general.chezmoi:
    repo: https://github.com/minnsoe/dotfiles
    state: latest

# As another user
- name: Ensure Alice's dotfiles are up-to-date against repo
  community.general.chezmoi:
    repo: https://github.com/minnsoe/dotfiles
    state: latest
  become: true
  become_user: alice

# With task defined config
- name: Create user dotfiles from repo given config
  community.general.chezmoi:
    repo: https://github.com/minnsoe/dotfiles
    state: latest
    config:
        data:
            full_name: Jane Austen
            email: example@example.com
'''

RETURN = r'''
#
'''
import hashlib
import json
import re
import os
import errno

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.facts.system.user import UserFactCollector

class Chezmoi(object):
    DEFAULT_CONFIG_RELATIVE_DIR = '.config/chezmoi/'
    DEFAULT_SOURCE_RELATIVE_DIR = '.local/share/chezmoi/'
    HOME_TMP_DIR = 'tmp'

    def __init__(
        self,
        module,
        exec_path,
        user_dir=None,
        repo=None,
        state=None,
        dest=None,
        source=None,
        config=None,
        purge=None,
        depth=None,
    ):
        self._module = module
        self._cmd = [module.get_bin_path('chezmoi', required=True, opt_dirs=exec_path)]
        self._user_dir = user_dir

        self._repo = repo
        self._state = state
        self._dest = dest
        self._source = source
        self._config = config
        self._config_path = None
        self._purge = purge
        self._depth = depth

    @property
    def global_flags(self):
        flags = ['--no-tty', '--force']

        if self._dest:
            flags.extend(['--destination', self._dest])

        if self._source:
            flags.extend(['--source', self._source])

        if self._config_path:
            flags.extend(['--config', self._config_path])

        return flags

    @property
    def default_source_dir(self):
        return os.path.join(self._user_dir, Chezmoi.DEFAULT_SOURCE_RELATIVE_DIR)

    @property
    def default_config_directory(self):
        return os.path.join(self._user_dir, Chezmoi.DEFAULT_CONFIG_RELATIVE_DIR)

    @property
    def home_temp_directory(self):
        return os.path.join(self._user_dir, Chezmoi.HOME_TMP_DIR)

    def _exec(self, args, with_global_flags=True, check_rc=False):
        cmd = self._cmd[:]

        if with_global_flags:
            cmd.extend(self.global_flags)

        if args:
            cmd.extend(args)

        return self._module.run_command(cmd, check_rc=check_rc)

    def get_data(self):
        return self._exec(['data'], check_rc=True)

    def _doctor(self, check_rc=False):
        _, out, _ = self._exec(['doctor'], check_rc=check_rc)

        # matches columns for 'RESULT CHECK MESSAGE'
        rows = re.findall('(\S+)\s+(\S+)\s+(.*)$', out, re.MULTILINE)
        if not len(rows) > 0:
            self._module.fail_json(msg='Failed to parse chezmoi doctor output.')

        checks = {}
        failed = {}
        for result, check, msg in rows[1:]:
            # store check infomation as a dict indexed by the name of the check
            checks[check] = {
                'result': result,
                'msg': msg,
            }

            # keep track of failed checks
            if result == 'error':
                failed[check] = checks[check]

        return {
            'failed': failed,
            'checks': checks,
        }

    def _apply(self, check_rc=True):
        status = self._status()

        if len(status) > 0:
            # some files do not match target, apply changes.
            self._exec(['apply'], check_rc=check_rc)

        return status

    def _source_present(self):
        source = self._source or self.default_source_dir
        return os.path.exists(source) and os.path.isdir(source)

    def _init(self):
        '''Init from repo'''

        cmd = ['init', self._repo]
        if self._depth:
            cmd.extend(['--depth', self._depth])

        return self._exec(cmd, check_rc=True)

    def _update(self):
        '''Pull changes from VCS and apply'''
        return self._exec(['update'], check_rc=True)

    def _purge_if_enabled(self):
        if self._purge:
            self._exec(['purge'], check_rc=True)
            return True
        return False

    def _create_module_defined_tmp_config(self):
        if not self._config:
            return

        # calculate content hash to use as filename
        h = hashlib.sha256()

        # dump config from task as sorted json for deterministic hash output
        file_contents = json.dumps(self._config, sort_keys=True)
        h.update(file_contents.encode('utf-8'))

        tmp_filename = 'ansible.{}.json'.format(h.hexdigest())
        file_path = os.path.join(self.default_config_directory, tmp_filename)

        # ensure directory exists
        try:
            os.makedirs(self.default_config_directory)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        # write if it does not exist
        if not os.path.exists(file_path):
            with open(file_path, 'w') as f:
                f.write(file_contents)

        self._config_path = file_path

    def _remove_module_defined_tmp_config(self):
        if os.path.exists(self._config_path):
            os.remove(self._config_path)

    def _status(self):
        _, out, _ = self._exec(['status'], check_rc=True)
        return list(filter(lambda i: bool(i), out.split('\n')))

    def ensure_present(self):
        return self.run(skip_init=self._source_present(), update=False)

    def ensure_latest(self):
        return self.run(update=True)

    def run(self, skip_init=False, update=False):
        msgs = []

        # create tmp config defined in task and init
        self._create_module_defined_tmp_config()

        if not skip_init:
            self._init()

        # check if installation is okay
        result = self._doctor(check_rc=False)
        if len(result['failed']) > 0:
            self._module.fail_json(changed=False, msg=result['failed'])

        if update:
            # update and apply changes
            self._update()
            msgs.append('Updated source to VCS latest and applied changes. ')
        else:
            # apply and check list of changes
            changes = self._apply()
            if changes:
                msgs.append('Made {} change(s) to match target state. '.format(len(changes)))

        # always clean up config created from task
        self._remove_module_defined_tmp_config()

        purged = self._purge_if_enabled()
        if purged:
            msgs.append('Purged chezmoi state and configuration. ')

        return dict(
            changed=True,
            original_message='',
            message=''.join(msgs).strip(),
            failed=False,
        )

    def ensure_absent(self):
        pass


def run_module():
    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        path=dict(type='path', required=False, default='/usr/local/bin/'),
        repo=dict(type='str', required=False),
        state=dict(type='str', required=True, choices=['present', 'absent', 'latest']),
        dest=dict(type='path', required=False),
        source=dict(type='path', required=False),
        config=dict(type='dict', required=False),
        purge=dict(type='bool', required=False, default=False),
    )

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=False)
    user_facts = UserFactCollector().collect()

    chezmoi = Chezmoi(
        module,
        exec_path=module.params['path'],
        user_dir=user_facts['user_dir'],
        repo=module.params['repo'],
        dest=module.params['dest'],
        source=module.params['source'],
        config=module.params['config'],
        state=module.params['state'],
        purge=module.params['purge'],
    )

    if module.params['state'] == 'latest':
        module.exit_json(**chezmoi.ensure_latest())

    if module.params['state'] == 'present':
        module.exit_json(**chezmoi.ensure_present())

    # TODO
    if module.check_mode:
        module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
