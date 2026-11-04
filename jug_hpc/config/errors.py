'''Errors raised while reading configuration files'''

__all__ = ['ConfigError']


class ConfigError(Exception):
    '''A configuration file is missing, unreadable or invalid

    The message always names the file (and, where it applies, the key) that
    caused the error, so it can be shown to the user as is.
    '''
