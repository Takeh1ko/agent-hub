-- Сколько раз событие отдавали оркестратору: повтор неподтверждённого — не больше MAX_DELIVERIES.
ALTER TABLE event ADD COLUMN deliveries INTEGER NOT NULL DEFAULT 0;
