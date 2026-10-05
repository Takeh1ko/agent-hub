-- T164: reasoning effort as its own choice (alias + effort). Additive: old code ignores the new columns.
ALTER TABLE task ADD COLUMN effort TEXT NOT NULL DEFAULT '';
UPDATE task SET executor='spark', effort='high' WHERE executor='spark-high';
UPDATE task SET executor='spark', effort='medium' WHERE executor='spark-medium';
UPDATE task SET executor='gemini', effort='low' WHERE executor='gemini-low';

ALTER TABLE session ADD COLUMN effort TEXT NOT NULL DEFAULT '';
UPDATE session SET model='spark', effort='high' WHERE model='spark-high';
UPDATE session SET model='spark', effort='medium' WHERE model='spark-medium';
UPDATE session SET model='gemini', effort='low' WHERE model='gemini-low';

-- Role menus store alias + effort: the key becomes (role, alias, effort); legacy names map to base + effort.
CREATE TABLE role_model_new (
  role TEXT NOT NULL,
  alias TEXT NOT NULL REFERENCES model(alias) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0,
  effort TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (role, alias, effort)
);
INSERT INTO role_model_new(role, alias, position, is_default, effort)
  SELECT role,
    CASE alias WHEN 'spark-high' THEN 'spark' WHEN 'spark-medium' THEN 'spark' WHEN 'gemini-low' THEN 'gemini' ELSE alias END,
    position, is_default,
    CASE alias WHEN 'spark-high' THEN 'high' WHEN 'spark-medium' THEN 'medium' WHEN 'gemini-low' THEN 'low' ELSE '' END
  FROM role_model;
DROP TABLE role_model;
ALTER TABLE role_model_new RENAME TO role_model;
