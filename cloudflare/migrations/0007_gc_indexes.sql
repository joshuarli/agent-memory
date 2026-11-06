CREATE INDEX IF NOT EXISTS generation_manifest_object ON generation(origin_host, manifest_object);
CREATE INDEX IF NOT EXISTS generation_object_plaintext ON generation_object(plaintext_digest);
CREATE INDEX IF NOT EXISTS snapshot_chunk_plaintext ON snapshot_chunk(plaintext_digest);
CREATE INDEX IF NOT EXISTS snapshot_chunk_ciphertext ON snapshot_chunk(ciphertext_digest);
CREATE INDEX IF NOT EXISTS snapshot_object_plaintext ON snapshot_object(plaintext_digest);
