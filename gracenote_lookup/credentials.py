"""App-scoped, local macOS Keychain storage. No file or fallback backend."""
import json
import sys

SERVICE = 'com.michaelbrandonfalk.gracenote-episode-lookup'
ACCOUNT = 'gracenote-view-sign-in'


class CredentialError(Exception):
    pass


class CredentialStore:
    def __init__(self, backend=None):
        self.backend = backend

    def keychain(self):
        if self.backend is None:
            if sys.platform != 'darwin':
                raise CredentialError('Credential storage requires macOS Keychain.')
            try:
                from keyring.backends.macOS import Keyring
                self.backend = Keyring()
            except Exception:
                raise CredentialError('macOS Keychain is unavailable. Credentials were not saved.') from None
        return self.backend

    def load(self):
        try:
            raw = self.keychain().get_password(SERVICE, ACCOUNT)
            if raw is None:
                return None
            record = json.loads(raw)
            if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ('username', 'password')):
                raise ValueError('Invalid record')
            return record
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Could not read the saved sign-in details. Check macOS Keychain access.') from None

    def save(self, username, password):
        username = username.strip()
        if not username or not password:
            raise CredentialError('Enter both a username and a password.')
        try:
            self.keychain().set_password(SERVICE, ACCOUNT, json.dumps({'username': username, 'password': password}))
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Could not save sign-in details in macOS Keychain. Nothing was exported to a file.') from None

    def remove(self):
        try:
            backend = self.keychain()
            if backend.get_password(SERVICE, ACCOUNT) is not None:
                backend.delete_password(SERVICE, ACCOUNT)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Could not remove the saved sign-in details. Check macOS Keychain access.') from None
