import unittest
from cardscanr_market_engine.native_pricing_browser_identity import verify_chrome_identity

class NativeBrowserIdentityTests(unittest.TestCase):
    def setUp(self):
        self.profile = r"C:\Users\owner\AppData\Local\CardScanR\PricingFixture"
        self.exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.kw = dict(listeners=[dict(address="127.0.0.1",port=9232,pid=42)],
                       processes=[dict(pid=42,session=1,executable=self.exe,
                           argv=[self.exe,"--user-data-dir="+self.profile,
                                 "--remote-debugging-port=9232",
                                 "--remote-debugging-address=127.0.0.1"])],
                       profile=self.profile,chrome_executable=self.exe,session_id=1)
    def check(self, reason):
        out = verify_chrome_identity(**self.kw)
        self.assertFalse(out["verified"])
        self.assertEqual(out["reason"],reason)
        self.assertFalse(out["marketplace_work_authorized"])
    def test_exact_owner(self):
        out = verify_chrome_identity(**self.kw)
        self.assertTrue(out["verified"])
        self.assertFalse(out["marketplace_work_authorized"])
    def test_reserved_port(self):
        self.kw["port"]=9231
        self.check("WRONG_PORT")
    def test_wsl_port(self):
        self.kw["port"]=9444
        self.check("WRONG_PORT")
    def test_external_address(self):
        self.kw["listeners"][0]["address"]="0.0.0.0"
        self.check("NON_LOOPBACK_LISTENER")
    def test_multiple_owners(self):
        self.kw["listeners"].append(dict(address="::1",port=9232,pid=43))
        self.check("MULTIPLE_LISTENER_OWNERS")
    def test_wrong_session(self):
        self.kw["processes"][0]["session"]=0
        self.check("WRONG_WINDOWS_SESSION")
    def test_wrong_executable(self):
        self.kw["processes"][0]["executable"]=r"C:\other\chrome.exe"
        self.check("WRONG_EXECUTABLE")
    def test_other_profile(self):
        self.kw["processes"][0]["argv"][1]+="-other"
        self.check("WRONG_PROFILE")
    def test_duplicate_profile(self):
        self.kw["processes"][0]["argv"].append("--user-data-dir="+self.profile)
        self.check("WRONG_PROFILE")
    def test_wildcard(self):
        self.kw["processes"][0]["argv"].append("--remote-allow-origins=*")
        self.check("UNAPPROVED_BROWSER_FLAGS")
    def test_headless(self):
        self.kw["processes"][0]["argv"].append("--headless")
        self.check("UNAPPROVED_BROWSER_FLAGS")
    def test_wrong_port_flag(self):
        self.kw["processes"][0]["argv"][2]="--remote-debugging-port=9230"
        self.check("WRONG_DEBUG_PORT")
    def test_missing_address(self):
        self.kw["processes"][0]["argv"].pop()
        self.check("WRONG_DEBUG_ADDRESS")
    def test_no_listener(self):
        self.kw["listeners"]=[]
        self.check("NO_LISTENER")
