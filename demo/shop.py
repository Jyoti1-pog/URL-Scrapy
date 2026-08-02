"""A local shop for checking haat-lister without touching anyone's real site.

    python demo/shop.py                       # leave this running
    haat-lister batch demo/urls.txt --provenance own

Serves three deliberately awkward kinds of product page on 127.0.0.1:8799:

  /plain/<n>      Server-rendered with clean JSON-LD. The easy case: Stage A
                  alone should get everything, and no browser should launch.

  /spa/<n>        A React-shaped shell. The static HTML is an empty <div> and a
                  <title> saying "Loading" -- the product data and the gallery
                  are injected by a script. Stage B should be the only reason
                  this row has a title.

  /hotlinked/<n>  Server-rendered, but its images 403 anyone arriving without a
                  Referer. This is the case Tier 1 predicate 7 exists for: the
                  photo loads fine in your browser on the product page and
                  would be broken for a buyer looking at a haat listing.

Everything is generated locally; nothing here reaches the internet.
"""

from __future__ import annotations

import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageFilter

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
PHOTO = pathlib.Path(__file__).parent / "product.jpg"

PRODUCTS = [
    ("Kutch mirror-work cotton kurta, hand-embroidered", "Hand-embroidered in Kutch, Gujarat "
     "on breathable handloom cotton, finished with mirror work along the yoke and cuffs."),
    ("Handwoven cotton stole with hand-knotted fringe", "Woven on a pit loom from unbleached "
     "cotton, with a hand-knotted fringe at both ends. Softens with every wash."),
    ("Brass jhumka earrings with filigree work", "Cast in brass and finished by hand with "
     "filigree detailing along the dome. Fitted with hypoallergenic ear wires."),
]


def product_json(index: int, kind: str) -> str:
    title, description = PRODUCTS[index % len(PRODUCTS)]
    return f"""{{
 "@context":"https://schema.org","@type":"Product",
 "name":{title!r},
 "description":{description!r},
 "image":["{BASE}/img/{kind}-{index}-hero.jpg","{BASE}/img/{kind}-{index}-detail.jpg"],
 "offers":{{"@type":"Offer","price":"2499","priceCurrency":"INR",
            "availability":"https://schema.org/InStock"}}
}}""".replace("'", '"')


SPECS = (
    "<table><tr><th>Weight</th><td>350 g</td></tr>"
    "<tr><th>Dimensions</th><td>L70 x W50 x H2 cm</td></tr></table>"
)


def plain_page(index: int, kind: str) -> bytes:
    title, _ = PRODUCTS[index % len(PRODUCTS)]
    return f"""<!doctype html><html><head>
<title>{title} | Demo Craft Co</title>
<script type="application/ld+json">{product_json(index, kind)}</script>
</head><body><h1>{title}</h1>
<img src="/img/{kind}-{index}-hero.jpg"><img src="/img/{kind}-{index}-detail.jpg">
{SPECS}</body></html>""".encode()


def spa_page(index: int) -> bytes:
    """Nothing useful in the HTML. Everything arrives when the script runs."""
    title, _ = PRODUCTS[index % len(PRODUCTS)]
    payload = product_json(index, "spa")
    return f"""<!doctype html><html><head><title>Loading&#8230;</title></head>
<body><div id="root"></div>
<script>
  var s = document.createElement('script');
  s.type = 'application/ld+json';
  s.textContent = {payload!r};
  document.head.appendChild(s);
  document.getElementById('root').innerHTML =
    '<h1>{title}</h1>' +
    '<img src="/img/spa-{index}-hero.jpg"><img src="/img/spa-{index}-detail.jpg">' +
    {SPECS!r};
</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002 -- stdlib signature
        print(f"  {self.command:4} {self.path}")

    def _send(self, body: bytes, ctype: str, head_only: bool, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False) -> None:
        # Tracking parameters are part of the point -- /plain/0?utm_source=x is
        # the same page as /plain/0, and canonicalisation should treat it so.
        path = self.path.split("?", 1)[0].split("#", 1)[0]

        if path.startswith("/img/"):
            # The hotlink-protected shop serves its own pages' visitors and
            # nobody else. Predicate 7 is what catches this.
            if "hotlinked" in path and not self.headers.get("Referer"):
                self._send(b"forbidden", "text/plain", head_only, status=403)
                return
            self._send(PHOTO.read_bytes(), "image/jpeg", head_only)
            return

        index = int(path.rstrip("/").rsplit("/", 1)[-1] or 0)
        if path.startswith("/spa/"):
            self._send(spa_page(index), "text/html; charset=utf-8", head_only)
        elif path.startswith("/hotlinked/"):
            self._send(plain_page(index, "hotlinked"), "text/html; charset=utf-8", head_only)
        else:
            self._send(plain_page(index, "plain"), "text/html; charset=utf-8", head_only)


def main() -> None:
    if not PHOTO.exists():
        print(f"Generating {PHOTO} ...")
        noise = Image.effect_noise((1600, 1600), 90).convert("L")
        Image.merge("RGB", [noise] * 3).filter(ImageFilter.GaussianBlur(2.0)).save(
            PHOTO, quality=92
        )

    print(f"Demo shop on {BASE}  (Ctrl-C to stop)\n")
    print("  /plain/N       clean JSON-LD, Stage A is enough")
    print("  /spa/N         JavaScript shell, needs Stage B")
    print("  /hotlinked/N   images 403 without a Referer, Tier 1 should catch it\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
