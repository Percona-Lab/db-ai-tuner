__version__ = "0.1.0"
try:  # the server stamps the git commit into the package it ships
    from ._build import BUILD
    __version__ += "+" + BUILD
except ImportError:
    pass
