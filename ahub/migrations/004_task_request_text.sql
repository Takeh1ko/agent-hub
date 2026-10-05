-- The text of a request to the owner: '' | 'stop' in `request`, the nudge message here.
ALTER TABLE task ADD COLUMN request_text TEXT NOT NULL DEFAULT '';
