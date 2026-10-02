-- How many times an event went to the orchestrator: unacked repeats cap at MAX_DELIVERIES.
ALTER TABLE event ADD COLUMN deliveries INTEGER NOT NULL DEFAULT 0;
