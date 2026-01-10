const express = require("express");
const fetch = require("node-fetch");
const cheerio = require("cheerio");

const app = express();
const PORT = process.env.PORT || 3000;

const TARGET_URL = "https://irres.be/te-koop";

app.get("/api/locations", async (req, res) => {
  try {
    const response = await fetch(TARGET_URL, {
      headers: {
        "User-Agent": "Mozilla/5.0"
      }
    });

    if (!response.ok) {
      return res.status(500).json({ error: "Failed to fetch website" });
    }

    const html = await response.text();
    const $ = cheerio.load(html);

    const locations = [];

    $("ul.search-values li[data-label]").each((_, el) => {
      const label = $(el).attr("data-label");
      if (label) locations.push(label);
    });

    res.json({
      details: [...new Set(locations)]
    });

  } catch (error) {
    res.status(500).json({
      error: "Unexpected error while scraping",
      message: error.message
    });
  }
});

app.get("/", (req, res) => {
  res.send("IRRES Locations API is running");
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
