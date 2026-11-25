"""Run from the repo folder:  python -m unittest discover tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import find  # noqa: E402

DEFAULTS = vars(find.build_parser().parse_args(["x.csv"]))


def run(text, encoding="utf-8", overrides=None, **opts):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "in.csv"
        p.write_bytes(text.encode(encoding))
        rows, stats = find.read_rows(p, overrides or {})
    o = dict(DEFAULTS)
    o.update(opts)
    cands, pairs, summary = find.analyse(rows, o)
    return rows, stats, cands, pairs, summary


BASIC = (
    "Query,Page,Clicks,Impressions,CTR,Position\n"
    "red widgets,https://example.com/a,60,1000,6%,4.0\n"
    "red widgets,https://example.com/b,40,800,5%,7.0\n"
    "blue widgets,https://example.com/c,10,500,2%,3.0\n"
)


class Reading(unittest.TestCase):
    def test_basic(self):
        rows, stats, cands, pairs, s = run(BASIC)
        self.assertEqual(len(rows), 3)
        self.assertEqual(s["candidates"], 1)
        c = cands[0]
        self.assertEqual(c["query"], "red widgets")
        self.assertEqual(c["severity"], "high")
        self.assertEqual(c["basis"], "clicks")
        self.assertEqual(c["pages"][0]["url"], "https://example.com/a")
        self.assertAlmostEqual(c["pages"][1]["share"], 0.4)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["kind"], "One-off overlap")

    def test_bom_and_excel_utf16(self):
        for enc in ("utf-8-sig", "utf-16"):
            rows, _, cands, _, _ = run(BASIC, encoding=enc)
            self.assertEqual(len(rows), 3, enc)
            self.assertEqual(len(cands), 1, enc)

    def test_semicolon_with_decimal_comma(self):
        text = ("Query;Landing Page;Url Clicks;Impressions;URL CTR;Average Position\n"
                "red widgets;https://example.com/a;60;1.000;6,0%;4,2\n"
                "red widgets;https://example.com/b;40;800;5,0%;7,5\n")
        rows, _, cands, _, _ = run(text)
        self.assertEqual(rows[0][3], 1000)
        self.assertEqual(rows[0][4], 4.2)
        self.assertEqual(cands[0]["pages"][1]["position"], 7.5)

    def test_tab_and_quoted_thousands(self):
        text = ('Top queries\tTop pages\tClicks\tImpressions\tPosition\n'
                'red widgets\thttps://example.com/a\t1,260\t12,000\t4\n'
                'red widgets\thttps://example.com/b\t40\t8 000\t7\n')
        rows, _, cands, _, _ = run(text)
        self.assertEqual(rows[0][2], 1260)
        self.assertEqual(rows[0][3], 12000)
        self.assertEqual(rows[1][3], 8000)
        self.assertEqual(len(cands), 1)

    def test_no_position_column(self):
        text = ("keyword,url,clicks,impressions\n"
                "red widgets,https://example.com/a,60,1000\n"
                "red widgets,https://example.com/b,40,800\n")
        _, stats, cands, pairs, _ = run(text)
        self.assertFalse(stats["has_position"])
        self.assertIsNone(cands[0]["pages"][0]["position"])
        self.assertEqual(cands[0]["note"], "")
        self.assertIsNone(pairs[0]["strong"]["position"])

    def test_date_and_device_rows_are_summed(self):
        text = ("Date,Device,Query,Page,Clicks,Impressions,Position\n"
                "2026-01-01,MOBILE,red widgets,https://example.com/a,30,100,2.0\n"
                "2026-01-02,DESKTOP,red widgets,https://example.com/a,30,300,6.0\n"
                "2026-01-01,MOBILE,Red  Widgets,https://example.com/b,20,200,9.0\n")
        _, _, cands, _, _ = run(text)
        a = cands[0]["pages"][0]
        self.assertEqual((a["clicks"], a["impressions"]), (60, 400))
        self.assertEqual(a["position"], 5.0)          # (2*100 + 6*300) / 400
        self.assertEqual(cands[0]["impressions"], 600)

    def test_missing_column_message(self):
        with self.assertRaises(find.InputError) as e:
            run("Top queries,Clicks,Impressions,CTR,Position\nx,1,2,3,4\n")
        self.assertIn("page", str(e.exception))
        self.assertIn("--page-col", str(e.exception))
        self.assertIn("same row", str(e.exception))

    def test_column_overrides(self):
        text = ("Suchanfrage,Seite,Klicks,Impressionen\n"
                "red widgets,https://example.com/a,60,1000\n"
                "red widgets,https://example.com/b,40,800\n")
        ov = {"query": "Suchanfrage", "page": "seite", "clicks": "Klicks", "impressions": "Impressionen"}
        _, _, cands, _, _ = run(text, overrides=ov)
        self.assertEqual(len(cands), 1)
        with self.assertRaises(find.InputError):
            run(text, overrides={"query": "Nope"})

    def test_hidden_queries_and_short_rows(self):
        text = BASIC + ",https://example.com/a,5,900,1%,3\n(not set),https://example.com/a,5,900,1%,3\nbroken row\n\n"
        _, stats, cands, _, _ = run(text)
        self.assertEqual(stats["blank_query"], 2)
        self.assertEqual(stats["bad_rows"], 1)
        self.assertEqual(len(cands), 1)


class Rules(unittest.TestCase):
    def test_url_variants_are_one_page(self):
        text = ("Query,Page,Clicks,Impressions,Position\n"
                "red widgets,https://example.com/a/,60,1000,4\n"
                "red widgets,http://EXAMPLE.com/a,20,500,5\n"
                "red widgets,https://example.com/a#faq,10,300,6\n")
        _, _, cands, _, _ = run(text)
        self.assertEqual(cands, [])
        _, _, cands, _, _ = run(text, keep_fragments=True)
        self.assertEqual(len(cands), 1)

    def test_params(self):
        text = ("Query,Page,Clicks,Impressions,Position\n"
                "red widgets,https://example.com/a?page=2,60,1000,4\n"
                "red widgets,https://example.com/a,20,500,5\n")
        self.assertEqual(len(run(text)[2]), 1)
        self.assertEqual(len(run(text, strip_params=True)[2]), 0)

    def test_brand_and_page_filters(self):
        text = BASIC + ("acme login,https://example.com/,90,1000,9%,1\n"
                        "acme login,https://example.com/login,10,900,1%,1\n"
                        "red widgets,https://example.com/fr/a,1,700,0%,20\n")
        self.assertEqual(run(text)[4]["candidates"], 2)
        _, _, cands, _, s = run(text, brand="Acme, other", exclude="/fr/")
        self.assertEqual([c["query"] for c in cands], ["red widgets"])
        self.assertEqual(len(cands[0]["pages"]), 2)
        self.assertEqual(s["brand_queries_skipped"], 1)
        self.assertEqual(s["filtered_rows"], 1)

    def test_thresholds(self):
        text = ("Query,Page,Clicks,Impressions,Position\n"
                "q1,https://example.com/a,5,30,4\nq1,https://example.com/b,5,15,5\n"      # under 50 in total
                "q2,https://example.com/a,50,950,4\nq2,https://example.com/b,1,50,30\n"    # 5% share
                "q3,https://example.com/a,0,60,4\nq3,https://example.com/b,0,9,5\n")       # under 10 on the page
        self.assertEqual(run(text)[2], [])
        self.assertEqual(len(run(text, min_impressions=10, min_share=0.01, min_page_impressions=1)[2]), 3)

    def test_severity_bands_and_impression_basis(self):
        text = ("Query,Page,Clicks,Impressions,Position\n"
                "few clicks,https://example.com/a,2,800,12\nfew clicks,https://example.com/b,1,200,15\n"
                "low one,https://example.com/a,88,800,3\nlow one,https://example.com/b,12,200,9\n")
        c = {x["query"]: x for x in run(text)[2]}
        self.assertEqual(c["few clicks"]["basis"], "impressions")
        self.assertEqual(c["few clicks"]["severity"], "medium")     # 20% of impressions
        self.assertIn("No page averages", c["few clicks"]["note"])
        self.assertEqual(c["low one"]["severity"], "low")           # 12% of clicks
        self.assertIn("double listing", c["low one"]["note"])

    def test_stronger_page_follows_the_basis(self):
        text = ("Query,Page,Clicks,Impressions,Position\n"
                "few clicks,https://example.com/lucky,1,50,9\n"
                "few clicks,https://example.com/big,0,400,6\n")
        _, _, cands, pairs, _ = run(text)
        c = cands[0]
        self.assertEqual(c["basis"], "impressions")
        self.assertEqual(c["pages"][0]["url"], "https://example.com/big")
        self.assertAlmostEqual(c["second_share"], 0.1111, places=3)
        self.assertEqual(c["severity"], "low")
        self.assertEqual(c["other_impressions"], 50)
        self.assertEqual(pairs[0]["strong"]["url"], "https://example.com/big")
        many = text.replace("lucky,1,50", "lucky,12,50")       # enough clicks: clicks decide
        _, _, cands, pairs, _ = run(many)
        self.assertEqual(cands[0]["basis"], "clicks")
        self.assertEqual(cands[0]["pages"][0]["url"], "https://example.com/lucky")
        self.assertEqual(pairs[0]["strong"]["url"], "https://example.com/lucky")

    def test_hidden_query_rows_count_towards_the_page_total(self):
        head = "Query,Page,Clicks,Impressions,Position\n"
        shared = "".join("q%d,https://example.com/a,60,1000,4\nq%d,https://example.com/b,40,800,7\n" % (i, i)
                         for i in range(3))
        self.assertEqual(run(head + shared)[3][0]["kind"], "Same topic")
        hidden = ",https://example.com/b,90,7200,5\n"           # b: 2400 shared of 9600 in total
        rows, stats, cands, pairs, s = run(head + shared + hidden)
        self.assertEqual(stats["blank_query"], 1)
        self.assertEqual(s["queries_checked"], 3)
        self.assertEqual(len(cands), 3)
        self.assertEqual(pairs[0]["weak_overlap"], 0.25)
        self.assertEqual(pairs[0]["kind"], "Partly overlapping")

    def test_pair_types(self):
        head = "Query,Page,Clicks,Impressions,Position\n"
        shared = "".join("q%d,https://example.com/a,60,1000,4\nq%d,https://example.com/b,40,800,7\n" % (i, i)
                         for i in range(3))
        p = run(head + shared)[3][0]
        self.assertEqual((p["kind"], p["shared_queries"], p["weak_overlap"]), ("Same topic", 3, 1.0))
        self.assertEqual(p["strong"]["url"], "https://example.com/a")
        own = "".join("own%d,https://example.com/b,50,5000,3\n" % i for i in range(3))
        self.assertEqual(run(head + shared + own)[3][0]["kind"], "Partly overlapping")


class Output(unittest.TestCase):
    NASTY = ("Query,Page,Clicks,Impressions,Position\n"
             '"</script><script>alert(1)</script>",https://example.com/a,60,1000,4\n'
             '"</script><script>alert(1)</script>","javascript:alert(2)",40,800,7\n'
             '"<img src=x onerror=alert(3)> & co",https://example.com/a,60,1000,4\n'
             '"<img src=x onerror=alert(3)> & co","https://example.com/b?x=""><b>",40,800,7\n')

    def build(self, text, name="my export.csv", **extra):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / name
            src.write_text(text, encoding="utf-8")
            out = Path(d) / "deep" / "report"
            code = find.main([str(src), "--out", str(out)] + [str(x) for kv in extra.items() for x in kv])
            self.assertEqual(code, 0)
            return ((out / "cannibalization.html").read_text(encoding="utf-8"),
                    (out / "queries.csv").read_text(encoding="utf-8"),
                    (out / "pairs.csv").read_text(encoding="utf-8"), d)

    def test_html_is_inert_and_carries_no_path(self):
        page, queries, pairs, tmp = self.build(self.NASTY)
        self.assertEqual(page.count("<script"), 2)
        self.assertEqual(page.count("</script>"), 2)
        self.assertNotIn("<img", page)
        self.assertNotIn("onerror=alert(3)>", page)
        self.assertNotIn(tmp, page)
        self.assertIn("my export.csv", page)
        self.assertIn("javascript:alert(2)", queries)
        self.assertEqual(len(queries.splitlines()), 5)
        self.assertEqual(len(pairs.splitlines()), 3)

    def test_html_row_cap(self):
        head = "Query,Page,Clicks,Impressions,Position\n"
        body = "".join("q%03d,https://example.com/a%d,60,1000,4\nq%03d,https://example.com/b%d,40,800,7\n"
                       % (i, i, i, i) for i in range(30))
        page, queries, _, _ = self.build(head + body, **{"--max-rows": 5})
        self.assertEqual(page.count('"query":"q0'), 5)
        self.assertIn('"queries_total":30', page)
        self.assertEqual(len(queries.splitlines()), 61)

    def test_errors_exit_2(self):
        self.assertEqual(find.main(["/nope/missing.csv"]), 2)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.csv"
            p.write_text("Top queries,Clicks,Impressions\nx,1,2\n")
            self.assertEqual(find.main([str(p), "--out", d]), 2)
            p.write_text("Query,Page,Clicks,Impressions\n")
            self.assertEqual(find.main([str(p), "--out", d]), 2)
            p.write_text("Query,Page,Clicks,Impressions\n,https://example.com/a,5,900\n")
            self.assertEqual(find.main([str(p), "--out", d]), 2)
            p.write_text(BASIC)
            self.assertEqual(find.main([str(p), "--out", d, "--exclude", "("]), 2)


if __name__ == "__main__":
    unittest.main()
