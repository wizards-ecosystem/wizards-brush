-- The Wizard's Brush 0.1.8 database (migration head 0011), representative rows.
-- Generated once by build_legacy_db.py; never regenerate after a migration lands.
BEGIN TRANSACTION;
CREATE TABLE applied_migrations (
               id          TEXT PRIMARY KEY,
               applied_at  TIMESTAMP NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW'))
           );
INSERT INTO "applied_migrations" VALUES('0001_baseline','2026-10-24 23:26:15.533');
INSERT INTO "applied_migrations" VALUES('0002_job_model_key','2026-10-24 23:26:15.534');
INSERT INTO "applied_migrations" VALUES('0003_asset_content','2026-10-24 23:26:15.535');
INSERT INTO "applied_migrations" VALUES('0004_asset_signals','2026-10-24 23:26:15.537');
INSERT INTO "applied_migrations" VALUES('0005_asset_human_grade','2026-10-24 23:26:15.538');
INSERT INTO "applied_migrations" VALUES('0006_job_remote_identity','2026-10-24 23:26:15.539');
INSERT INTO "applied_migrations" VALUES('0007_job_request_id','2026-10-24 23:26:15.540');
INSERT INTO "applied_migrations" VALUES('0008_variant_sets','2026-10-24 23:26:15.541');
INSERT INTO "applied_migrations" VALUES('0009_job_client','2026-10-24 23:26:15.542');
INSERT INTO "applied_migrations" VALUES('0010_recipes','2026-10-24 23:26:15.543');
INSERT INTO "applied_migrations" VALUES('0011_recipe_defaulted','2026-10-24 23:26:15.544');
CREATE TABLE asset (
	id INTEGER NOT NULL, 
	kind VARCHAR NOT NULL, 
	path VARCHAR NOT NULL, 
	filename VARCHAR NOT NULL, 
	thumb VARCHAR, 
	width INTEGER, 
	height INTEGER, 
	job_id INTEGER, 
	generator VARCHAR NOT NULL, 
	meta_json VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	favorite BOOLEAN NOT NULL, 
	rating INTEGER NOT NULL, 
	grade_json VARCHAR NOT NULL, 
	tags_json VARCHAR NOT NULL, 
	caption VARCHAR NOT NULL, 
	deleted_at DATETIME, 
	content_id INTEGER, 
	mtime_ns INTEGER, 
	is_missing BOOLEAN NOT NULL, 
	needs_verify BOOLEAN NOT NULL, 
	enrich_level INTEGER NOT NULL, 
	used_count INTEGER NOT NULL, 
	last_used_at DATETIME, 
	embedding BLOB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(content_id) REFERENCES assetcontent (id)
);
INSERT INTO "asset" VALUES(1,'image','/legacy/output/images/1.png','1.png',NULL,1024,1024,1,'local_image:txt2img','{"_v": 1, "prompt": "a fox in snow, soft watercolour washes", "raw_prompt": "a fox in snow", "seed": 1234, "model_variant": "turbo", "loras": [{"path": "ink_wash.safetensors", "weight": 0.8}], "post": [{"processor": "background_removal"}]}','2026-10-20 10:00:00.000000',1,0,'{}','[]','',NULL,1,NULL,0,0,0,0,NULL,NULL);
INSERT INTO "asset" VALUES(2,'image','/legacy/output/images/2.png','2.png',NULL,1024,1024,NULL,'mask','{"_v": 1}','2026-10-20 10:00:00.000000',0,0,'{}','[]','',NULL,2,NULL,0,0,0,0,NULL,NULL);
INSERT INTO "asset" VALUES(3,'image','/legacy/output/images/3.png','3.png',NULL,1024,1024,3,'local_image:inpaint','{"_v": 1, "prompt": "a red scarf", "seed": 7}','2026-10-20 10:00:00.000000',0,0,'{}','[]','',NULL,3,NULL,0,0,0,0,NULL,NULL);
INSERT INTO "asset" VALUES(4,'image','/legacy/output/images/4.png','4.png',NULL,1024,1024,5,'upscale','{"_v": 1, "post": [{"operation": "upscale", "scale": 2}]}','2026-10-20 10:00:00.000000',0,0,'{}','[]','',NULL,4,NULL,0,0,0,0,NULL,NULL);
INSERT INTO "asset" VALUES(5,'image','/legacy/output/images/5.png','5.png',NULL,1024,1024,7,'colab_edit','{"_v": 1, "prompt": "the same character, standing", "variant": {"set_id": 1, "item_id": 1}}','2026-10-20 10:00:00.000000',0,0,'{}','[]','',NULL,5,NULL,0,0,0,0,NULL,NULL);
CREATE TABLE assetcontent (
	id INTEGER NOT NULL, 
	hash VARCHAR NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	mime_type VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "assetcontent" VALUES(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1001,'image/png','2026-10-20 10:00:00.000000');
INSERT INTO "assetcontent" VALUES(2,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',1002,'image/png','2026-10-20 10:00:00.000000');
INSERT INTO "assetcontent" VALUES(3,'cccccccccccccccccccccccccccccccccccccccc',1003,'image/png','2026-10-20 10:00:00.000000');
INSERT INTO "assetcontent" VALUES(4,'dddddddddddddddddddddddddddddddddddddddd',1004,'image/png','2026-10-20 10:00:00.000000');
INSERT INTO "assetcontent" VALUES(5,'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',1005,'image/png','2026-10-20 10:00:00.000000');
CREATE TABLE collection (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "collection" VALUES(1,'Foxes','2026-10-20 10:00:00.000000');
CREATE TABLE collectionitem (
	id INTEGER NOT NULL, 
	collection_id INTEGER NOT NULL, 
	asset_id INTEGER NOT NULL, 
	added_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "collectionitem" VALUES(1,1,1,'2026-10-20 10:00:00.000000');
CREATE TABLE job (
	id INTEGER NOT NULL, 
	kind VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	progress FLOAT NOT NULL, 
	message VARCHAR NOT NULL, 
	params_json VARCHAR NOT NULL, 
	result_json VARCHAR NOT NULL, 
	error VARCHAR NOT NULL, 
	priority INTEGER NOT NULL, 
	group_id VARCHAR, 
	request_id VARCHAR, 
	model_key VARCHAR, 
	remote_client_id VARCHAR, 
	remote_token VARCHAR, 
	client VARCHAR NOT NULL, 
	tip VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id)
);
INSERT INTO "job" VALUES(1,'image_local','done',1.0,'','{"_v": 1, "prompt": "a fox in snow, soft watercolour washes", "raw_prompt": "a fox in snow", "style_ids": ["watercolor"], "negative_prompt": "", "quality": "Standard", "loras": [{"path": "ink_wash.safetensors", "weight": 0.8}], "sampler": "default", "steps": 9, "guidance": 0.0, "seed": 1234, "seed_mode": "increment", "batch": 1, "width": 1024, "height": 1024, "aspect": "1:1", "strength": 0.6, "finish": "none", "post_upscale": false, "post_face": false, "post_scale": 4, "post_detail": false, "detail_prompt": "", "combinatorial": false, "speed_mode": false, "prompt_syntax": "literal", "model_variant": "turbo", "finish_steps": [{"processor": "background_removal"}, {"processor": "resize", "width": 512, "height": 512, "mode": "contain", "background": "transparent"}]}','{"asset_ids": [1]}','',0,NULL,'fixture-request-000000000001','Tongyi-MAI/Z-Image-Turbo',NULL,NULL,'ui','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(2,'image_colab','queued',0.0,'','{"_v": 1, "prompt": "a lighthouse at dusk", "raw_prompt": "a lighthouse at dusk", "quality": "High", "steps": 30, "guidance": 4.0, "seed": 99, "batch": 2, "width": 1328, "height": 1328, "aspect": "1:1", "model_variant": "quality", "finish": "none"}','{}','',0,NULL,NULL,'Qwen/Qwen-Image-2512',NULL,NULL,'mcp','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(3,'inpaint','done',1.0,'','{"_v": 1, "prompt": "a red scarf", "raw_prompt": "a red scarf", "quality": "Standard", "steps": 9, "guidance": 0.0, "seed": 7, "batch": 1, "width": 1024, "height": 1024, "aspect": "Match input", "strength": 0.6, "inpaint_area": "masked area", "mask_grow": 4, "mask_padding": 64, "mask_blur": 4, "model_variant": "turbo", "image_path": "/legacy/output/uploads/up_a.png", "mask_path": "/legacy/output/uploads/up_mask.png"}','{"asset_ids": [3]}','',0,NULL,NULL,'Tongyi-MAI/Z-Image-Turbo',NULL,NULL,'ui','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(4,'image_edit','error',0.0,'','{"_v": 1, "prompt": "make the jacket red", "image_paths": ["/legacy/output/images/a.png"], "quality": "Standard", "steps": 30, "guidance": 4.0, "seed": 5, "input_fidelity": "standard", "background": "opaque"}','{}','Traceback: boom',0,NULL,NULL,NULL,NULL,NULL,'api','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(5,'upscale','done',1.0,'','{"_v": 1, "scale": 2, "src_path": "/legacy/output/images/a.png", "source_asset_id": 1}','{"asset_ids": [4]}','',0,NULL,NULL,NULL,NULL,NULL,'ui','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(6,'image_local','done',1.0,'','{"prompt": "an old portrait", "steps": 9, "guidance": 1.0, "seed": 42, "post_upscale": true, "post_scale": 2, "model_variant": "turbo"}','{"asset_ids": []}','',0,NULL,NULL,NULL,NULL,NULL,'','','2026-10-20 10:00:00.000000',NULL,NULL);
INSERT INTO "job" VALUES(7,'image_edit','done',1.0,'','{"_v": 1, "prompt": "the same character, standing", "image_paths": ["/legacy/output/images/a.png"], "seed": 11, "variant": {"set_id": 1, "item_id": 1, "key": "pose=standing"}}','{"asset_ids": [5]}','',0,'vset-00000000000000000000000000000001','vset-0f0f0f0f0f0f-item-1-attempt-1',NULL,NULL,NULL,'ui','','2026-10-20 10:00:00.000000',NULL,NULL);
CREATE TABLE prompthistory (
	id INTEGER NOT NULL, 
	kind VARCHAR NOT NULL, 
	prompt VARCHAR NOT NULL, 
	negative VARCHAR NOT NULL, 
	params_json VARCHAR NOT NULL, 
	favorite BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "prompthistory" VALUES(1,'image_local','a fox in snow','','{"_v": 1, "prompt": "a fox in snow"}',0,'2026-10-20 10:00:00.000000');
CREATE TABLE recipe (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	name VARCHAR NOT NULL, 
	slug VARCHAR NOT NULL, 
	description VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	params_json VARCHAR NOT NULL, 
	fields_json VARCHAR NOT NULL, 
	inputs_json VARCHAR NOT NULL, 
	defaulted_json VARCHAR NOT NULL, 
	client VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL
);
INSERT INTO "recipe" VALUES(1,'Ceramic cup','ceramic-cup','','image_local','{"prompt": "a ceramic cup", "quality": "Standard", "guidance": 0.0, "seed": -1, "model_variant": "turbo"}','[{"name": "prompt", "type": "textarea", "label": "Prompt", "description": "", "default": "a ceramic cup", "constraints": {}}]','{"images": [], "mask": null, "last_frame": null}','["guidance", "seed"]','ui','2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
INSERT INTO "recipe" VALUES(2,'Jacket recolour','jacket-recolour','','image_edit','{"prompt": "make the jacket red", "quality": "Standard"}','[{"name": "prompt", "type": "textarea", "label": "Prompt", "description": "", "default": "make the jacket red", "constraints": {}}]','{"images": [1], "mask": null, "last_frame": null}','[]','mcp','2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
CREATE TABLE userpreset (
	id INTEGER NOT NULL, 
	type VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	payload_json VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	preview_asset_id INTEGER, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "userpreset" VALUES(1,'style','Watercolour','{"text": "soft watercolour washes"}','all',NULL,'2026-10-20 10:00:00.000000');
CREATE TABLE variantitem (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	set_id INTEGER NOT NULL, 
	stage INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	"key" VARCHAR NOT NULL, 
	values_json VARCHAR NOT NULL, 
	parent_item_id INTEGER, 
	source_asset_ids_json VARCHAR NOT NULL, 
	params_json VARCHAR NOT NULL, 
	job_id INTEGER, 
	attempts INTEGER NOT NULL, 
	history_json VARCHAR NOT NULL, 
	asset_ids_json VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	state_reason VARCHAR NOT NULL, 
	validation_state VARCHAR NOT NULL, 
	validation_json VARCHAR NOT NULL, 
	output_name VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL
);
INSERT INTO "variantitem" VALUES(1,1,0,0,'pose=standing','{"pose": "standing"}',NULL,'[1]','{}',7,1,'[]','[5]','succeeded','','pending','[]','poses/1','2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
INSERT INTO "variantitem" VALUES(2,1,0,1,'pose=sitting','{"pose": "sitting"}',NULL,'[1]','{}',NULL,0,'[]','[]','pending','','pending','[]','poses/2','2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
CREATE TABLE variantrecipe (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	name VARCHAR NOT NULL, 
	description VARCHAR NOT NULL, 
	config_json VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL
);
INSERT INTO "variantrecipe" VALUES(1,'Poses','','{"source_asset_ids": [1], "stages": [{"operation": "image_edit", "axes": [{"name": "pose", "values": ["standing", "sitting"]}], "prompt": "the same character, {{pose}}"}]}','2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
CREATE TABLE variantset (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	name VARCHAR NOT NULL, 
	recipe_id INTEGER, 
	recipe_json VARCHAR NOT NULL, 
	operation VARCHAR NOT NULL, 
	source_asset_ids_json VARCHAR NOT NULL, 
	group_id VARCHAR NOT NULL, 
	request_id VARCHAR, 
	status VARCHAR NOT NULL, 
	expected INTEGER NOT NULL, 
	counts_json VARCHAR NOT NULL, 
	collection_id INTEGER, 
	replay_json VARCHAR NOT NULL, 
	canceled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL
);
INSERT INTO "variantset" VALUES(1,'Poses',1,'{"source_asset_ids": [1], "stages": [{"operation": "image_edit", "axes": [{"name": "pose", "values": ["standing", "sitting"]}], "prompt": "the same character, {{pose}}"}]}','image_edit','[1]','vset-00000000000000000000000000000001',NULL,'active',2,'{}',NULL,'{}',NULL,'2026-10-20 10:00:00.000000','2026-10-20 10:00:00.000000');
CREATE INDEX ix_job_remote_token ON job (remote_token);
CREATE UNIQUE INDEX ix_job_request_id ON job (request_id);
CREATE INDEX ix_job_status ON job (status);
CREATE INDEX ix_job_created_at ON job (created_at);
CREATE INDEX ix_job_remote_client_id ON job (remote_client_id);
CREATE INDEX ix_job_group_id ON job (group_id);
CREATE INDEX ix_job_model_key ON job (model_key);
CREATE INDEX ix_job_priority ON job (priority);
CREATE UNIQUE INDEX ix_assetcontent_hash ON assetcontent (hash);
CREATE INDEX ix_prompthistory_favorite ON prompthistory (favorite);
CREATE INDEX ix_prompthistory_created_at ON prompthistory (created_at);
CREATE INDEX ix_collection_created_at ON collection (created_at);
CREATE INDEX ix_collectionitem_asset_id ON collectionitem (asset_id);
CREATE INDEX ix_collectionitem_collection_id ON collectionitem (collection_id);
CREATE INDEX ix_userpreset_created_at ON userpreset (created_at);
CREATE UNIQUE INDEX ix_recipe_slug ON recipe (slug);
CREATE INDEX ix_recipe_updated_at ON recipe (updated_at);
CREATE INDEX ix_recipe_created_at ON recipe (created_at);
CREATE INDEX ix_variantrecipe_created_at ON variantrecipe (created_at);
CREATE INDEX ix_variantset_recipe_id ON variantset (recipe_id);
CREATE UNIQUE INDEX ix_variantset_request_id ON variantset (request_id);
CREATE UNIQUE INDEX ix_variantset_group_id ON variantset (group_id);
CREATE INDEX ix_variantset_status ON variantset (status);
CREATE INDEX ix_variantset_created_at ON variantset (created_at);
CREATE INDEX ix_variantitem_parent_item_id ON variantitem (parent_item_id);
CREATE INDEX ix_variantitem_state ON variantitem (state);
CREATE INDEX ix_variantitem_job_id ON variantitem (job_id);
CREATE INDEX ix_variantitem_set_id ON variantitem (set_id);
CREATE UNIQUE INDEX ux_variantitem_set_stage_key ON variantitem (set_id, stage, "key");
CREATE INDEX ix_asset_created_at ON asset (created_at);
CREATE INDEX ix_asset_job_id ON asset (job_id);
CREATE INDEX ix_asset_deleted_at ON asset (deleted_at);
CREATE INDEX ix_asset_is_missing ON asset (is_missing);
CREATE INDEX ix_asset_content_id ON asset (content_id);
CREATE INDEX ix_asset_favorite ON asset (favorite);
CREATE INDEX ix_asset_enrich_level ON asset (enrich_level);
CREATE INDEX ix_asset_rating ON asset (rating);
CREATE INDEX ix_asset_used_count ON asset (used_count);
CREATE INDEX ix_job_dequeue ON job (status, priority DESC, id ASC);
CREATE UNIQUE INDEX ux_job_request_id ON job (request_id);
CREATE UNIQUE INDEX ux_recipe_name_nocase ON recipe (name COLLATE NOCASE);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('recipe',2);
INSERT INTO "sqlite_sequence" VALUES('variantrecipe',1);
INSERT INTO "sqlite_sequence" VALUES('variantset',1);
INSERT INTO "sqlite_sequence" VALUES('variantitem',2);
COMMIT;
