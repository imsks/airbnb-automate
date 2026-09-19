"""Airbnb Automate — an agent office for content-for-stay collaborations.

Third-party deprecation warnings are silenced here rather than at a call site:
Google's client libraries emit Python-version FutureWarnings the moment they are
imported, which happens lazily inside ``get_llm``. Package import is the only
point that reliably runs first.
"""

import warnings

for _module in ("google", "google.auth", "google.oauth2", "google.api_core", "urllib3"):
    warnings.filterwarnings("ignore", category=FutureWarning, module=rf"{_module}.*")
    warnings.filterwarnings("ignore", category=DeprecationWarning, module=rf"{_module}.*")

warnings.filterwarnings("ignore", message=r".*Python version.*", category=FutureWarning)
warnings.filterwarnings("ignore", message=r".*non-supported Python version.*")
warnings.filterwarnings("ignore", message=r".*OpenSSL.*")
