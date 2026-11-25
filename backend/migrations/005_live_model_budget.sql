CREATE TABLE live_model_usage (
    usage_date date PRIMARY KEY,
    calls integer NOT NULL CHECK (calls > 0)
);
