-- Owner-approved public starter source. Registration does not publish a build.
INSERT INTO mika_sources (name, url, enabled, created_at, updated_at)
SELECT '0xRadikal Top 100',
       'https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/top100.txt',
       1, datetime('now'), datetime('now')
WHERE NOT EXISTS (
  SELECT 1 FROM mika_sources
  WHERE url = 'https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/top100.txt'
);
