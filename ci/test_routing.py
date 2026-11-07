import unittest
from routing import ROUTE, updated_config


class RoutingTests(unittest.TestCase):
    def test_only_adds_new_route_and_is_idempotent(self):
        original = 'listen: ( { host: "0.0.0.0"; port: "443"; } );\nprotocols: (\n  { name: "tls"; host: "127.0.0.1"; port: "4446"; sni_hostnames: [ "servizi.everbright.it" ]; },\n);\n'
        updated = updated_config(original)
        self.assertEqual(updated.replace("\n" + ROUTE, "", 1), original)
        self.assertEqual(updated_config(updated), updated)

    def test_unexpected_existing_route_is_rejected(self):
        with self.assertRaises(ValueError):
            updated_config(ROUTE.replace("4447", "4448"))

    def test_unexpected_format_is_rejected(self):
        with self.assertRaises(ValueError):
            updated_config("invalid configuration")
