-- question.project: whose question it is (an orchestrator sees the questions of its project only);
-- the rows of the old schema take the project of their task, a question without a task stays hub-wide.
ALTER TABLE question ADD COLUMN project TEXT NOT NULL DEFAULT '';
UPDATE question SET project = COALESCE((SELECT t.project FROM task t WHERE t.id = question.task_id), '');
CREATE INDEX idx_question_project ON question(project, status);
