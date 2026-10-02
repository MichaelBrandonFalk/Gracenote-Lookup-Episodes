import json
import unittest
from unittest.mock import patch

from gracenote_lookup.credentials import ACCOUNT, SERVICE, CredentialError, CredentialStore


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[service, account] = password

    def delete_password(self, service, account):
        del self.values[service, account]


class CredentialTests(unittest.TestCase):
    def test_roundtrip_and_remove_use_only_the_app_scoped_vault_item(self):
        vault = Vault()
        vault.values[('another-app', 'another-account')] = 'untouched'
        store = CredentialStore(vault)
        self.assertIsNone(store.load())
        store.save(' example@example.com ', 'synthetic-test-password')
        self.assertEqual(store.load(), {'username': 'example@example.com', 'password': 'synthetic-test-password'})
        self.assertEqual(set(vault.values), {(SERVICE, ACCOUNT), ('another-app', 'another-account')})
        store.remove()
        store.remove()  # Removing an absent item is harmless.
        self.assertIsNone(store.load())
        self.assertEqual(vault.values, {('another-app', 'another-account'): 'untouched'})

    def test_backend_errors_never_include_credential_contents(self):
        class Broken(Vault):
            def set_password(self, service, account, password):
                raise RuntimeError(password)
        with self.assertRaises(CredentialError) as result:
            CredentialStore(Broken()).save('private-username', 'private-password')
        self.assertNotIn('private-', str(result.exception))

    def test_no_plaintext_fallback_on_other_platforms(self):
        with patch('gracenote_lookup.credentials.sys.platform', 'linux'):
            with self.assertRaisesRegex(CredentialError, 'requires macOS Keychain'):
                CredentialStore().save('example', 'synthetic-password')

    def test_reject_empty_and_corrupt_records(self):
        vault = Vault()
        store = CredentialStore(vault)
        for user, password in [('', 'password'), ('user', ''), ('  ', 'password')]:
            with self.assertRaises(CredentialError):
                store.save(user, password)
        for raw in ['not json', '[]', json.dumps({'username': 'example', 'password': 5})]:
            vault.values[(SERVICE, ACCOUNT)] = raw
            with self.assertRaises(CredentialError):
                store.load()
