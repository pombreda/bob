#
# Copyright (c) 2008 rPath, Inc.
#
# All rights reserved.
#

'''
Mechanism for expanding macros from a trove context.
'''

import logging

def expand(raw, parent, trove=None):
    '''Transform a raw string with available configuration data.'''
    macros = {}

    # Basic info
    macros.update(parent.cfg.macro)
    for cfg_item in ('tag',):
        macros[cfg_item] = getattr(parent.cfg, cfg_item)

    # Additional info available in trove contexts
    if trove:
        if trove in parent.targets:
            hg = parent.targets[trove].hg
            if hg and parent.hg.has_key(hg):
                macros['hg'] = parent.hg[hg][1]
            elif hg:
                logging.warning('Trove %s references undefined Hg '
                    'repository %s', trove, hg)

    return raw % macros
