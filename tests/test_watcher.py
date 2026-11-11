import http.server, json, os, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watcher

def ld(price, avail="https://schema.org/InStock", extra=""):
    d = {"@context": "https://schema.org", "@type": "Product", "name": "Quest 3S",
         "offers": {"@type": "Offer", "priceCurrency": "GBP", "price": price,
                    "availability": avail}}
    return '<html><head><script type="application/ld+json">%s</script></head></html>' % json.dumps(d)

PAGES = {
    "/jsonld": ld("299.00"),
    "/graph": '<script type="application/ld+json">{"@graph":[{"@type":"Product","offers":{"@type":"AggregateOffer","lowPrice":"1,299.50"}}]}</script>',
    "/meta": '<html><head><meta property="og:price:amount" content="289.99"></head></html>',
    "/itemprop": '<span itemprop="price" content="305.00">£305</span>',
    "/oos": ld("249.00", "https://schema.org/OutOfStock"),
    "/regex": '<div class="p">Now <b>£279.99</b></div>',
    "/junk": "<html>Access denied</html>",
}

class H(http.server.BaseHTTPRequestHandler):
    seen = []
    def do_GET(self):
        H.seen.append(dict(self.headers))
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404); self.end_headers()
        self.wfile.write((body or "not found").encode())
    def log_message(self, *a): pass

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def get(self, path, rx=None):
        return watcher.check_target({"url": self.base + path, "price_regex": rx})

    def test_jsonld(self):
        r, _ = self.get("/jsonld"); self.assertEqual((r["price"], r["in_stock"], r["source"]), (299.0, True, "json-ld"))
    def test_graph_lowprice(self):
        r, _ = self.get("/graph"); self.assertEqual(r["price"], 1299.5)
    def test_meta(self):
        r, _ = self.get("/meta"); self.assertEqual((r["price"], r["source"]), (289.99, "meta:og:price:amount"))
    def test_itemprop(self):
        r, _ = self.get("/itemprop"); self.assertEqual(r["price"], 305.0)
    def test_oos(self):
        r, _ = self.get("/oos"); self.assertIs(r["in_stock"], False)
    def test_regex(self):
        r, _ = self.get("/regex", r"£\s*([\d,.]+)"); self.assertEqual((r["price"], r["source"]), (279.99, "regex"))
    def test_unparseable(self):
        r, e = self.get("/junk"); self.assertIsNone(r); self.assertIn("no price", e)
    def test_404(self):
        r, e = self.get("/nope"); self.assertIsNone(r); self.assertEqual(e, "HTTP 404")
    def test_headers(self):
        self.get("/jsonld"); h = H.seen[-1]
        self.assertIn("Mozilla", h["User-Agent"]); self.assertTrue(h["Accept-Language"].startswith("en-GB"))
    def test_fallback(self):
        r, _ = watcher.check_target({"url": self.base + "/nope", "fallback_url": self.base + "/jsonld"})
        self.assertEqual(r["source"], "json-ld (fallback)")

class Flow(unittest.TestCase):
    """End-to-end run_once with notify mocked."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        watcher.STATE_PATH = os.path.join(self.d, "state.json")
        watcher.CONFIG_PATH = os.path.join(self.d, "config.json")
        self.sent = []
        self._orig = (watcher.fetch, watcher.notify)
        watcher.notify = lambda m, c, dry=False, title="": self.sent.append((m, c)) or True
        watcher.fetch = lambda url, timeout=25: self.page
    def tearDown(self):
        watcher.fetch, watcher.notify = self._orig
    def cfg(self, **t):
        t.setdefault("name", "X"); t.setdefault("url", "http://x/y")
        with open(watcher.CONFIG_PATH, "w") as f: json.dump({"defaults": {"target_price": 299, "drop_percent": 10}, "targets": [t,
                   {"name": "bad", "url": "ftp://nope"}]}, f)
    def run_(self, page): self.page = page; watcher.run_once()

    def test_alert_new_low_and_rearm(self):
        self.cfg()
        self.run_(ld("350")); self.assertEqual(len(self.sent), 0)
        self.run_(ld("299")); self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], "http://x/y")
        self.run_(ld("299")); self.assertEqual(len(self.sent), 1)   # same price
        self.run_(ld("289")); self.assertEqual(len(self.sent), 2)   # new low
        self.run_(ld("350")); self.run_(ld("299")); self.assertEqual(len(self.sent), 3)  # re-armed
    def test_percent_drop(self):
        self.cfg(target_price=100)
        self.run_(ld("400")); self.run_(ld("361")); self.assertEqual(len(self.sent), 0)
        self.run_(ld("360")); self.assertEqual(len(self.sent), 1)   # exactly 90% of high
    def test_oos_no_alert(self):
        self.cfg(); self.run_(ld("200", "OutOfStock")); self.assertEqual(self.sent, [])
    def test_blocked_after_5(self):
        self.cfg()
        for _ in range(7): self.run_("<html>captcha</html>")
        self.assertEqual(len(self.sent), 1); self.assertIn("5 checks", self.sent[0][0])
        self.run_(ld("500")); 
        for _ in range(5): self.run_("<html>captcha</html>")
        self.assertEqual(len(self.sent), 2)  # rearmed after a success

class Dedupe(Flow):
    def test_fallback_alerts_once(self):
        t = [{"name": n, "url": "http://x/" + n, "fallback_url": "http://ps/p"} for n in "abc"]
        with open(watcher.CONFIG_PATH, "w") as f:
            json.dump({"defaults": {"target_price": 299}, "targets": t}, f)
        watcher.fetch = lambda url, timeout=25: ld("250") if "ps" in url else (_ for _ in ()).throw(OSError("blocked"))
        watcher.run_once(); self.assertEqual(len(self.sent), 1)
        watcher.run_once(); self.assertEqual(len(self.sent), 1)

if __name__ == "__main__":
    unittest.main()
