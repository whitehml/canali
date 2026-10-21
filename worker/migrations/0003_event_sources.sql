-- Retain the latest FIRST payloads with the prepared input in one atomic write.
ALTER TABLE event_input ADD COLUMN source_document TEXT
    CHECK (source_document IS NULL OR json_valid(source_document));
