import os
import psutil
import duckdb
import threading
import subprocess
import argparse
import time
import duckdb
from tableauhyperapi import HyperProcess, Telemetry, Connection, CreateMode


TPCH_DATABASE = "tpch-sf100.duckdb"
HYPER_DATABASE = "tpch-sf100.hyper"

VALID_SYSTEMS = ['duckdb', 'hyper']

DROP_ANSWER_SQL = "Drop table if exists ans;"

HYPER_FAILING_OPERATOR_QUERIES = [
'aggr-l_orderkey-l_partkey.sql',
'aggr-l_orderkey-l_suppkey.sql',
'aggr-l_suppkey-l_partkey-l_orderkey.sql',
'aggr-l_suppkey-l_partkey-l_shipinstruct.sql',
'aggr-l_suppkey-l_partkey-l_returnflag-l_linestatus.sql',
'aggr-l_suppkey-l_partkey-l_shipinstruct-l_shipmode.sql',
'aggr-l_suppkey-l_partkey-l_shipmode.sql',
'hash-join-large.sql'
]

def get_mem_lock_file(query_file):
    return query_file.replace('.sql', '_lock')

def get_mem_usage_db_file(benchmark_name, benchmark):
    return benchmark_name + "/" + benchmark + "/data.duckdb"

def create_mem_poll_lock(query_file):
    file_name = query_file.replace('.sql', '_lock')
    try:
        # Open the file in write mode, creating it if it doesn't exist
        # it is a lock file so we don't do anything once the file is created.
        with open(file_name, 'w'):
            pass
        return
    except Exception as e:
        print(f"Error: {e}")

def stop_polling_mem(query_file):
    try:
        # Remove the file
        file_name = query_file.replace('.sql', '_lock')
        os.remove(file_name)
    except FileNotFoundError:
        print(f"Error: File '{file_name}' not found.")
    except Exception as e:
        print(f"Error: {e}")


def start_polling_mem(query_file, system, benchmark_name, benchmark, run, hyper_pid):
    def run_script():
        try:
            mem_db = get_mem_usage_db_file(benchmark_name, benchmark)

            if not os.path.exists(benchmark_name + "/" + benchmark):
                os.makedirs(f"{benchmark_name}/{benchmark}")

            # create db if it does not yet exist.
            if not os.path.exists(mem_db):
                con = duckdb.connect(mem_db)
                with open('utils/data_schema.sql') as f: schema = f.read()
                con.sql(f"{schema}")
                con.close()

            query = query_file.replace('.sql', '')
            mem_lock_file = get_mem_lock_file(query_file)

            args = ['python3', 'utils/poll_process_mem.py', mem_db, mem_lock_file, benchmark_name, benchmark, system, run, query, str(hyper_pid)]
            
            # Run the script using subprocess.Popen
            subprocess.run(args, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error running script: {e}")

    create_mem_poll_lock(query_file)
    # Create a new thread and run the script inside it
    script_thread = threading.Thread(target=run_script)
    script_thread.start()

def get_query_from_file(file_name):
    try:
        # Open the file in read mode and read the contents
        with open(file_name, 'r') as file:
            query = file.read()
            return query
    except FileNotFoundError:
        print(f"Error: File '{file_name}' not found.")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


def run_query(query_file, system, memory_limit, benchmark_name, benchmark):
    if query_file in HYPER_FAILING_OPERATOR_QUERIES:
        print(f"hyper fails, skipping query")
        return
    
    if system == "duckdb":
        run_duckdb_hot_cold(query_file, memory_limit, benchmark_name, benchmark)
    elif system == "hyper":
        run_hyper_hot_cold(query_file, memory_limit, benchmark_name, benchmark)
    else:
        print("System must be hyper or duckdb")
        exit(1)

def run_duckdb_hot_cold(query_file, memory_limit, benchmark_name, benchmark):

    try:
        con = duckdb.connect(TPCH_DATABASE)

        if memory_limit > 0:
            memory_limit_str = f"'{memory_limit}GB'"
            con.sql(f"SET memory_limit={memory_limit_str}")

        query = get_query_from_file(f"benchmark-queries/{benchmark}-queries/{query_file}")
        pid = os.getpid()
        for run in ["cold", "hot"]:
            print(f"{run} run")
            if benchmark == 'operators' and query_file.find("join") >= 1:
                con.sql(DROP_ANSWER_SQL)
                time.sleep(3)
            # Create a cursor to execute SQL queries
            start_polling_mem(query_file, "duckdb", benchmark_name, benchmark, run, pid)
            # Execute the query.
            if benchmark == 'operators' and query_file.find("join") >= 1:
                # join operators save the data, so .sql is enough
                con.sql(query)
            else:
                # other benchmarks need .execute so that all data is processed in duckdb
                con.sql(query).execute()
            # stop polling memory
            stop_polling_mem(query_file)
            time.sleep(4)

        if benchmark == 'operators' and query_file.find("join") >= 1:
            con.sql(DROP_ANSWER_SQL)
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        con.close()
    print(f"done.")
    time.sleep(5)

def run_hyper_hot_cold(query_file, memory_limit, benchmark_name, benchmark):
    db_path = f"{HYPER_DATABASE}"

    memory_limit_str = f"{memory_limit}g"
    if memory_limit == 0:
        # default value as quoted here https://help.tableau.com/current/server/en-us/cli_configuration-set_tsm.htm?_gl=1*1lb2mz5*_ga*NjExMDIxMzgzLjE3MDAyMjE1Mjc.*_ga_8YLN0SNXVS*MTcwNDgwMTAwNC40LjEuMTcwNDgwMjE1OC4wLjAuMA
        memory_limit_str = "80%"

    process_parameters = {"default_database_version": "2", "memory_limit": memory_limit_str}
    query = get_query_from_file(f"benchmark-queries/{benchmark}-queries/{query_file}")
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters=process_parameters) as hyper:
        with Connection(hyper.endpoint, db_path, CreateMode.CREATE_IF_NOT_EXISTS) as con:
            current_process = psutil.Process()
            children = current_process.children(recursive=True)
            if len(children) > 1:
                print("hyper has too many child processes. aborting")
                exit(0)

            hyper_pid = children[0].pid
                
            for run in ["cold", "hot"]:
                print(f"{run} run")
                if benchmark == 'operators':
                    con.execute_command(DROP_ANSWER_SQL)
                    time.sleep(3)
                start_polling_mem(query_file, "hyper", benchmark_name, benchmark, run, hyper_pid)
                res = con.execute_command(query)
                stop_polling_mem(query_file)
                time.sleep(4)
            if benchmark == 'operators':
                con.execute_command(DROP_ANSWER_SQL)
    print(f"done.")
    time.sleep(5)

def profile_query_mem(query_file, systems, memory_limit, benchmark_name, benchmark):
    for system in systems:
        print(f"profiling memory for {system}. query {query_file}")
        run_query(query_file, system, memory_limit, benchmark_name, benchmark)
        print(f"done profiling")

def get_query_file_names(benchmark):
    # Get the absolute path to the specified directory
    directory_path = os.path.abspath(f"./benchmark-queries/{benchmark}-queries/")

    # Initialize an empty list to store file names
    file_list = []

    try:
        # List all files in the directory
        files = os.listdir(directory_path)

        # Filter out directories, keep only files
        file_list = [file for file in files if os.path.isfile(os.path.join(directory_path, file))]
    except FileNotFoundError:
        # Handle the case where the directory does not exist
        print(f"Error: Directory '{directory_path}' not found.")

    file_list.sort()
    return file_list



def parse_args_and_setup(args):
    benchmark_name = "benchmarks/" + args.benchmark_name
    
    if args.system not in ["hyper", "duckdb", "all"]:
        print("Usage: python3 utils/run_benchmark.py --benchmark_name=[name] --benchmark=[tpch|aggr-thin|aggr-wide|join] --system=[duckdb|hyper|all]")
        exit(1)

    benchmarks = args.benchmark.split(",")
    if args.benchmark == 'all':
        benchmarks = ['tpch', 'operators']


    memory_limit = args.memory_limit

    systems = args.system.split(",")
    if len(systems) == 0:
        print("please pass valid system names. Valid systems are " + VALID_SYSTEMS)

    if systems[0] == "all":
        systems = ["duckdb", "hyper"]

    for system_ in systems:
        if system_ not in VALID_SYSTEMS:
            print("please pass valid system names. Valid systems are " + VALID_SYSTEMS)

    if benchmark_name is None:
        # create benchmark name
        print("please pass benchmark name")
        exit(1)

    return benchmark_name, benchmarks, systems, memory_limit


def main(args):
    benchmark_name, benchmarks, systems, memory_limit = parse_args_and_setup(args)

    overwrite = False
    if os.path.isdir(benchmark_name):
        print(f"benchmark {benchmark_name} already exists. Going to overwrite")
        overwrite = True
    else:
        os.makedirs(benchmark_name)

    for benchmark in benchmarks:
        query_file_names = get_query_file_names(benchmark)
        
        mem_db = get_mem_usage_db_file(benchmark_name, benchmark)
        if overwrite and os.path.exists(mem_db):
            os.remove(mem_db)

        for query_file in query_file_names:
            profile_query_mem(query_file, systems, memory_limit, benchmark_name, benchmark)

        # write the duckdb to csv 
        
        con = duckdb.connect(mem_db)
        csv_result_file_duckdb = f"{benchmark_name}/{benchmark}-duckdb-results"
        csv_result_file_hyper = f"{benchmark_name}/{benchmark}-hyper-results"
        con.sql(f"copy time_info to '{csv_result_file_duckdb}.csv' (FORMAT CSV, HEADER 1)")
        con.sql(f"copy proc_mem_info to '{csv_result_file_hyper}.csv' (FORMAT CSV, HEADER 1)")
        # os.remove(mem_db)
        con.close()



def run_all_queries():
    parser = argparse.ArgumentParser(description='Run tpch on hyper or duckdb')

    # Add command-line arguments
    parser.add_argument('--benchmark_name', type=str, help='Specify the benchmark name. Benchmark files are stored in this directory')
    parser.add_argument('--benchmark', type=str, help='list of benchmarks to run. \'all\', \'tpch\', etc.')
    parser.add_argument('--system', type=str, help='System to benchmark. Either duckdb or hyper')
    parser.add_argument('--memory_limit', type=int, help="memory limit for both systems", default=0)

    # Parse the command-line arguments
    args = parser.parse_args()
    main(args)



if __name__ == "__main__":
    run_all_queries()
