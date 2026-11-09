"""Select only an explicitly frozen, qualified built-in deletion mode."""


def module_for(scope):
    paseo, t3 = scope.get('paseo_only'), scope.get('t3code_only')
    if paseo and t3:
        raise ValueError('builtin_delete_modes_conflict')
    if paseo:
        from . import paseo_daemon_delete
        return paseo_daemon_delete
    if t3:
        from . import t3code_server_delete
        return t3code_server_delete
    return None
