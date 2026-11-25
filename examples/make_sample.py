#!/usr/bin/env python3
"""Writes examples/sample-export.csv: a made-up Search Console export for a made-up
houseplant shop on example.com. Nothing in it is real data.

    python examples/make_sample.py
"""
import csv
import random
from pathlib import Path

SITE = "https://www.example.com"
random.seed(7)
rows = []


def add(query, page, impressions, ctr, position):
    clicks = int(round(impressions * ctr))
    rows.append([query, SITE + page, clicks, impressions,
                 "%.2f%%" % (100.0 * clicks / impressions if impressions else 0), "%.1f" % position])


def split(queries, strong, weak, weak_share, strong_pos, weak_pos, base=(300, 2400)):
    """Two pages share every query in the list."""
    for q in queries:
        total = random.randint(*base)
        w = int(total * random.uniform(weak_share * 0.8, weak_share * 1.2))
        add(q, strong, total - w, random.uniform(0.03, 0.07), strong_pos + random.uniform(-1, 1))
        add(q, weak, w, random.uniform(0.01, 0.04), weak_pos + random.uniform(-1.5, 1.5))


# 1. Same topic: an old blog post and a newer guide answer the same question.
split(["how to repot a monstera", "repotting monstera", "when to repot monstera",
       "monstera repotting guide", "best soil for repotting monstera", "repot monstera deliciosa",
       "monstera root bound signs", "how often to repot monstera", "repotting large monstera"],
      "/guides/repotting-monstera/", "/blog/how-to-repot-a-monstera/", 0.42, 7.5, 13.5)

# 2. Partly overlapping: a blog list and a shop collection.
split(["low light plants", "best low light plants", "low light indoor plants",
       "plants for dark rooms", "low light houseplants"],
      "/collections/low-light-plants/", "/blog/best-low-light-plants/", 0.33, 5.0, 8.0)
for q, imp in [("low light plants for bathroom", 900), ("low light hanging plants", 700),
               ("plants that need no sunlight", 1600), ("low light plants safe for cats", 1100),
               ("low light office plants", 800), ("easy plants for beginners", 2100)]:
    add(q, "/blog/best-low-light-plants/", imp, 0.05, 6.2)

# 3. Partly overlapping: a care guide and a narrower watering post.
split(["snake plant watering", "how often to water snake plant", "snake plant overwatering"],
      "/blog/snake-plant-watering/", "/blog/snake-plant-care/", 0.24, 4.5, 11.0)
for q, imp in [("snake plant care", 3800), ("snake plant light needs", 1200),
               ("snake plant brown tips", 950), ("snake plant propagation", 1700)]:
    add(q, "/blog/snake-plant-care/", imp, 0.06, 5.1)

# 4. Double listing: both pages sit high for the same query. Often fine.
split(["pothos vs philodendron"], "/blog/pothos-vs-philodendron/", "/blog/pothos-care/", 0.36, 2.8, 4.9,
      base=(2500, 2600))
for q, imp in [("pothos care", 4200), ("pothos yellow leaves", 1500), ("pothos light requirements", 900)]:
    add(q, "/blog/pothos-care/", imp, 0.07, 4.0)

# 5. One-off overlaps and a low-severity case.
split(["fiddle leaf fig dropping leaves"], "/blog/fiddle-leaf-fig-problems/", "/blog/fiddle-leaf-fig-care/",
      0.30, 7.0, 13.0)
split(["calathea curling leaves"], "/blog/calathea-care/", "/blog/why-are-my-plant-leaves-curling/",
      0.13, 5.5, 14.0)
split(["terracotta vs plastic pots"], "/blog/terracotta-vs-plastic-pots/", "/collections/terracotta-pots/",
      0.17, 6.0, 12.0)

# 6. The same page reported with a #section. It is merged into its parent page.
add("snake plant watering", "/blog/snake-plant-care/#watering", 140, 0.02, 9.0)
add("how to repot a monstera", "/guides/repotting-monstera/#step-by-step", 210, 0.04, 5.5)

# 7. Brand searches: several pages show as sitelinks. Skip them with --brand leafmoor.
for q, imp in [("leafmoor", 5200), ("leafmoor plants", 1900), ("leafmoor discount code", 600)]:
    add(q, "/", int(imp * 0.6), 0.45, 1.0)
    add(q, "/collections/all/", int(imp * 0.25), 0.05, 1.4)
    add(q, "/about/", int(imp * 0.15), 0.02, 1.8)

# 8. A language folder that ranks for the same English query. Exclude it with --exclude "/fr/".
add("low light plants", "/fr/collections/plantes-faible-lumiere/", 260, 0.01, 22.0)

# 9. Healthy queries with one page each.
SINGLES = ["zz plant care", "peace lily drooping", "rubber plant pruning", "spider plant babies",
           "aloe vera repotting", "orchid watering schedule", "best pots for succulents",
           "fungus gnats in soil", "bottom watering plants", "plant humidity tray",
           "grow lights for houseplants", "self watering pots", "leaf shine spray",
           "string of pearls care", "monstera adansonii care", "bird of paradise indoor care"]
for q in SINGLES:
    add(q, "/blog/%s/" % q.replace(" ", "-"), random.randint(400, 3000), random.uniform(0.03, 0.08),
        random.uniform(3, 9))
    add(q, "/blog/houseplant-care-basics/", random.randint(3, 9), 0.0, random.uniform(30, 60))

# 10. Rows where Search Console hides the query.
add("", "/blog/pothos-care/", 800, 0.05, 6.0)
add("", "/guides/repotting-monstera/", 650, 0.04, 7.0)

random.shuffle(rows)
out = Path(__file__).parent / "sample-export.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Query", "Landing Page", "Url Clicks", "Impressions", "URL CTR", "Average Position"])
    w.writerows(rows)
print("%d rows -> %s" % (len(rows), out.name))
