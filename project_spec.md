## Operation Dynamic Python MCP template

## Goal

We aspire to create a boiler plate scaffold template small collection of python scripts to be rapidly deployed to any linux or windows environment to run a recursive AI agent. For the purposes of this hypothetical we must think outside the box. The agents we will create from this template could be deployed for any number of mission critical purposes, for example enterprise security or acting as simple cooking timers.  Some will be setup as Cron jobs, others will be setup as daemons or windows services, by default the agent starts in interactive mode allowing a user to type in the terminal to interact with it. We don't know where our agent will be deployed so versatility is paramount. 
## Tool calling models

Our agent must leverage loaded large language models loaded through either kobold, ollama or external apis, such as;

Gemma has been installed and uses roughly 3GB of VRAM.
gemma-4-E4B-it-Q5_K_S.gguf

## Dynamic prompts
All system prompts for our agents are stored in the sqlite database in the agent_prompts sqlite table allowing us to easily switch between stored system prompts dynamically at runtime.  at run time in our index.py we wish to retrieve all prompts in the agent_prompts sqlite table, our default prompt prompt_name would be named default.

## Roster of functions
functions are stored within the sqlite database in the functions table, upon launch in index.py, at run time all functions within this table are loaded into memory which will then be sequentially appended to our system prompt

## Execution Flow

Our index.py file is the core of our framework. We aspire to keep it small and condensed, our project should not consist of more than six files.

1. index.py should invoke our db_seed.py python script, db_seed.py ensures the database.db SQLITE database file exists, if not, it should create the database, ensuring the tables exist and seed the newly created tables using db_seed.py

2. Once we've established we have a sqlite table, we should open and iterate through the settings_boolean, settings_values, functions and agent_prompts tables recording the contents of these tables into their own individual arrays.

3. Once index.py has gathered all of the relevant database tables and populated the arrays, it should iterate through settings_values and settings_binaries arrays.  we should ensure that our booleans in our setting_bool tables are 0 or 1, otherwise throw an error. We should check the contents of settings_values to ensure there are no large strings or crazy values to prevent buffer overflowers. Basic data sanitization.

4. index.py should should then search our settings_values array for DEFAULT_PROMPT and retrieve the value, which is default, we then use the value of the DEFAULT_PROMPT in the settings_values array as a search criteria to retrieve the requisite prompt from the agent_prompts sqlite table.

5. once we have our desired system prompt, we wish to iterate through the array of functions we extracted from the database retrieving the function_name and function_description.  We do not want the function_body at this point as we're compiling a digest of available functions that our agent has access to. We append this list of functions to our System prompt providing the agent a roster of functions which it can use.

6. By default the agent will act in interactive mode in the future we aspire to give it directives allowing it to autonomously act using the functions provided.


## File structure

## File name rules - simple file names, no sub folders.

## /home/kim/projects/template

index.py (root file, responsible for assigning constants and importing python modules and running loops)
db.py (functions pertaining to sqlite database access)
db_seed.py (functions pertaining to populating the sqlite database, creating non existant tables and pruning the database when desired)
db_functions.py (contains seed functions to populate the functions sqlite table)
database.db 

## Desired Database schema

functions (id auto increment, function_name TEXT, functions_description TEXT, function_body TEXT, function_language TEXT, function_created DATETIME, function_modified DATETIME, function_enabled INTEGER DEFAULT 1)
settings_boolean (id auto increment, setting_name TEXT UNIQUE, setting_bool INTEGER DEFAULT 1)
settings_values (id auto increment, setting_name TEXT UNIQUE, setting_value TEXT)
agent_prompts (id auto increment, prompt_name TEXT, prompt_body TEXT, prompt_enabled DEFAULT 1)
logs (id auto increment, log_code INTEGER DEFAULT 1, log_text TEXT, log_date DATETIME)


## Applications Installed

Kobold
Ollama
AnythingLLM

## Models sub folder location
/media/storage/g/AI/models

## Modules Installed
mcp
sqlite
pytorch
cuda
pip
firejail

## Hardware
GPU: GTX 3060 (12GB VRAM)
RAM: 64 GB (DDR 4)
CPU: I7 Skylake 6700 (3.4 Ghz)

## Environment
Lubuntu with LXQT

## Project root folder
/home/kim/projects/template

## Kobold Endpoint URL
http://localhost:5001/api/v1/generate

## Flexibility

index.py - constants we expect to see the following constants.

DB_PATH             = "database.db"
DEFAULT_PROMPT 		= "DEFAULT" # defined by prompt_name in the agent_prompts sqlite table
MCP_PORT			= "8206"
ENDPOINT_KOBOLD     = "http://localhost:5001/api/v1/generate"
ENDPOINT_OLLAMA 	= "http://localhost:11434/api/generate"
ANTI_PROMPTS     = ["User:", "<|im_end|>", "\n\n\n"]

KOBOLD_MAX_TOKENS  = 1024
KOBOLD_TEMPERATURE = 0.1
KOBOLD_TOP_P       = 0.9
Context Size: 16000

## Future Goals
Merge Queues
Conflict detection
Resolution protocols

Supervision mode (agents that monitor and course correct other agents)
Financial observability (how much did this agent spend)
Standard failure patterns (what happens when a tool call fails)

## Firejail commands

firejail --whitelist=/home/kim/projects/template python3 /home/kim/projects/template/index.py
