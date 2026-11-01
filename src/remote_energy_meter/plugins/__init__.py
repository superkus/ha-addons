"""Device plugin package.

Presence of a submodule in this package *is* the registration: ``Plugin_Registry``
discovers plugins by walking ``__path__`` with :func:`pkgutil.iter_modules` and
collecting module-level classes that satisfy the ``DevicePlugin`` protocol. No plugin
module needs to call a register function.

``__path__`` is extended with :func:`pkgutil.extend_path` so a plugin shipped by a
separate distribution can contribute modules to ``remote_energy_meter.plugins`` and be
discovered by the same walk, without changing the metering core.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
