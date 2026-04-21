# System Rearchitecture Plan

## Goal

Refactor the current system into a clearer execution architecture with persistent storage in PostgreSQL:

- `skill`
- `capability`
- `provider`
- `environment`

Execution chain:

- `skill -> capability -> provider@environment`

At the same time, improve testing from the current limited command execution model into a sandbox-aware workflow with preparation, execution, evidence collection, and cleanup.

## Target Architecture

### Skill

Responsibilities:

- methodology
- workflow orchestration
- trigger hints
- dependency declaration on capabilities

Non-responsibilities:

- direct execution
- provider selection details
- environment isolation details

Examples:

- `testing.strategy`
- `coding.incremental_delivery`
- imported ClawHub instruction-only skills
- workflow-oriented skills such as `obsidian`

### Capability

Capabilities should remain atomic and bottom-layer execution primitives.

Near-term retained or introduced capabilities:

- `code.edit:v1`
- `command.run.safe:v1`
- `file.read:v1`
- `file.write:v1`
- `env.prepare:v1`
- `service.start:v1`
- `service.stop:v1`
- `artifact.collect:v1`

Capabilities explicitly not kept as default built-ins unless backed by real execution:

- `doc.read:v1`
- `doc.write:v1`
- `asset.generate:v1`
- requirements/design/delivery prompt-level actions

### Provider

Concrete execution backend for a capability.

Supported provider types:

- `builtin`
- `local_tool`
- `http_api`
- `mcp_server`

Examples:

- `command.run.safe:v1` -> local tool provider
- `file.read:v1` -> builtin or MCP provider
- future real `doc.read` / `doc.write` -> HTTP or MCP provider

### Environment

Execution isolation, permissions, dependency preparation, lifecycle management.

Initial environment types:

- `host_safe`
- `task_sandbox`
- `containerized`

Sandbox is not a separate layer. It is an environment implementation.

## Current Pain Points

### Skill / Capability Boundary Is Blurry

Prompt engineering and workflow semantics are mixed into capability definitions, which makes the capability catalog inaccurate.

### Capability / Binding Coupling

The current `catalog + bindings` structure is not expressive enough for:

- multiple providers
- reusable providers
- skill-to-capability dependency modeling
- environment selection

### Testing Is Too Limited

Current testing suffers from:

- narrow command allowlist
- no dependency preparation phase
- no task-level isolated environment
- no service lifecycle handling
- weak test profiles

### ClawHub Import Semantics Are Unclear

The system currently lacks a clear distinction between:

- instruction-only skills
- skills depending on existing capabilities
- skills requiring external tools, HTTP services, or MCP servers

## PostgreSQL Persistence Design

All long-lived skill/capability/provider/environment data should move from JSON config files to PostgreSQL.

### Proposed Tables

#### skills

- `skill_id`
- `name`
- `source`
- `kind`
- `description`
- `content_md`
- `trigger_hints`
- `enabled`
- `version`
- `metadata_json`
- timestamps

#### capabilities

- `capability_id`
- `name`
- `category`
- `description`
- `kind`
- `builtin_handler`
- `input_schema_json`
- `output_schema_json`
- `enabled`
- `version`
- `metadata_json`
- timestamps

#### providers

- `provider_id`
- `name`
- `provider_type`
- `config_json`
- `auth_config_json`
- `enabled`
- `version`
- `metadata_json`
- timestamps

#### environments

- `environment_id`
- `name`
- `environment_type`
- `config_json`
- `allowed_commands_json`
- `allow_install`
- `allow_network`
- `enabled`
- timestamps

#### skill_capability_dependencies

- `dependency_id`
- `skill_id`
- `capability_id`
- `dependency_type`
- `notes`
- timestamps

#### capability_provider_bindings

- `binding_id`
- `capability_id`
- `provider_id`
- `environment_id`
- `priority`
- `defaults_json`
- `enabled`
- timestamps

## Testing Improvement Design

Testing should be implemented as a skill-driven workflow instead of a facade capability.

### Testing Skill

Introduce:

- `testing.strategy`

Responsibilities:

- determine which tests should run
- decide whether preparation or service startup is required
- handle retry / fallback strategy
- interpret failures and collect evidence requirements

### Test Workflow

`testing.strategy` orchestrates atomic capabilities:

1. `env.prepare`
2. `command.run.safe`
3. `service.start` if needed
4. `command.run.safe` for test/build/lint/typecheck/smoke commands
5. `artifact.collect`
6. `service.stop`

### Test Profiles

Near-term target support:

- `unit`
- `integration`
- `smoke`
- `build`
- `lint`
- `typecheck`

Later:

- `e2e`

### task_sandbox

Initial sandbox requirements:

- per-task workspace isolation
- isolated temp directories
- isolated Python virtualenv
- isolated Node dependency directory
- configurable command allowlist
- cleanup of spawned processes and temporary resources

### Commands to Open in task_sandbox

Python:

- `python`
- `python3`
- `pip`
- `pytest`
- `coverage`
- `ruff`
- `mypy`

Node:

- `node`
- `npm`
- `pnpm`
- `yarn`
- `npx`
- `vitest`
- `jest`
- `playwright`
- `tsc`
- `vite`

General:

- `bash`
- `sh`
- `make`
- `ls`
- `cat`

## ClawHub Skill Import Strategy

Imported skills should be classified into three groups:

1. instruction-only skill
2. skill depending on existing capabilities
3. skill requiring external provider or MCP support

Import should produce:

- skill definition
- capability dependency records
- provider requirement hints
- install guidance

Examples:

- `word-docx` -> instruction-only skill
- `obsidian` -> workflow skill requiring external local-tool provider

## Implementation Plan

### Phase 1: Model Refactor

- reduce default capability catalog to real bottom-layer capabilities
- add PostgreSQL tables for skill/capability/provider/environment
- separate dependency and binding data from capability definitions

### Phase 2: Persistence Migration

- add PostgreSQL DAO/service methods
- dual-write with existing JSON config as temporary compatibility layer
- switch reads to PostgreSQL
- remove JSON as primary storage path after stabilization

### Phase 3: Provider and Environment Layer

- implement provider and environment APIs
- add initial provider types: builtin, local_tool, mcp_server
- add environments: host_safe, task_sandbox

### Phase 4: Capability Atomicization

- preserve and harden `code.edit:v1`
- preserve and harden `command.run.safe:v1`
- add `file.read:v1`
- add `file.write:v1`
- add `env.prepare:v1`
- add `service.start:v1`
- add `service.stop:v1`
- add `artifact.collect:v1`

### Phase 5: Testing Upgrade

- introduce `testing.strategy`
- implement `prepare -> execute -> collect -> teardown`
- route testing through `task_sandbox`
- expand supported command surface in sandbox

### Phase 6: ClawHub Import Refactor

- import skills into PostgreSQL
- classify import type automatically
- store skill dependencies instead of backfilling capability catalog

## Immediate Next Steps

1. Persist this plan and current system state.
2. Push the current system version to git.
3. Start with PostgreSQL persistence skeleton for:
   - skills
   - capabilities
   - providers
   - environments
   - dependency / binding relations
4. Add first-pass API endpoints for provider/environment management.
5. Begin replacing JSON-backed skill/capability storage with PostgreSQL-backed services.
