import contextlib
import ctypes
import functools
import itertools
import logging
import multiprocessing
import os
import sqlite3
import sys
import time
from services.parser.ast_node_identifier import ASTNodeId
from services.parser.clang_parser import ChildVisitResult
from services.parser.clang_parser import ImmutableSourceLocation

# TODO move this to utils
from itertools import izip_longest
def slice_it(iterable, n, padvalue=None):
    return izip_longest(*[iter(iterable)]*n, fillvalue=padvalue)

class ClangIndexer(object):
    def __init__(self, parser, callback = None):
        self.callback = callback
        self.db = None
        self.indexer_directory_name = '.indexer'
        self.indexer_db_name = 'indexer.db'
        self.cpu_count = multiprocessing.cpu_count()
        self.proj_root_directory = None
        self.compiler_args = None
        self.parser = parser
        self.op = {
            0x0 : self.__run_on_single_file,
            0x1 : self.__run_on_directory,
            0x2 : self.__drop_single_file,
            0x3 : self.__drop_all,
            0x10 : self.__go_to_definition,
            0x11 : self.__find_all_references
        }


    def __call__(self, args):
        self.op.get(int(args[0]), self.__unknown_op)(int(args[0]), args[1:len(args)])

    def __unknown_op(self, id, args):
        logging.error("Unknown operation with ID={0} triggered! Valid operations are: {1}".format(id, self.op))

    def __run_on_single_file(self, id, args):
        proj_root_directory = str(args[0])
        contents_filename = str(args[1])
        original_filename = str(args[2])
        compiler_args = str(args[3])

        self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))

        if contents_filename == original_filename:
            self.__index_single_file(proj_root_directory, contents_filename, original_filename, compiler_args, self.db)
            self.db.commit()

        if self.callback:
            self.callback(id, args)

    def __run_on_directory(self, id, args):
        # NOTE  Indexer will index each file in directory in a way that it will:
        #           1. Index a file
        #           2. Flush its AST immediately to the disk
        #           3. Repeat 1 & 2 for each file
        #
        #       One might notice that 2nd step could have been:
        #           1. Run after whole directory has been indexed
        #              (which is possible because we keep all the translation units in memory)
        #           2. Skipped and executed on demand through a separate API (if and when client wants to)
        #
        #       Both approaches have been evaluated and it turned out that 'separate API' approach lead to
        #       very high RAM consumption (>10GB) which would eventually render the indexer non-functional
        #       for any mid- to large-size projects.
        #
        #       For example, running an indexer on a rather smallish project (cppcheck, ~330 files at this moment)
        #       would result in:
        #           1. RAM consumption of ~5GB if we would parse all of the files _AND_ flush the ASTs to the disk.
        #              The problem here is that RAM consumption would _NOT_ go any lower even after the ASTs have been
        #              flushed to disk which was strange enough ...
        #           2. RAM consumption of ~650MB if we would load all of the previously parsed ASTs from the disk.
        #       There is a big discrepency between these two numbers which clearly show that there is definitely some
        #       memory lost in the process.
        #
        #       Analysis of high RAM consumption has shown that issue was influenced by a lot of small object artifacts
        #       (small memory allocations), which are:
        #           1. Generated by the Clang-frontend while running its parser.
        #           2. Still laying around somewhere in memory even after parsing has been completed.
        #           3. Accumulating in size more and more the more files are parsed.
        #           4. Not a subject to memory leaks according to the Valgrind but rather flagged as 'still reachable' blocks.
        #           5. Still 'occupying' a process memory space even though they have been 'freed'.
        #               * It is a property of an OS memory allocator to decide whether it will or it will not swap this memory
        #                 out of the process back to the OS.
        #               * It does that in order to minimize the overhead/number of dynamic allocations that are potentially
        #                 to be made in near future and, hence, reuse already existing allocated memory chunk(s).
        #               * Memory allocator can be forced though to claim the memory back to the OS through
        #                 'malloc_trim()' call if supported by the OS, but this does not guarantee us to get to
        #                  the 'original' RAM consumption.
        #
        #       'Flush-immeditelly-after-parse' approach seems to not be having these issues and has a very low memory
        #       footprint even with the big-size projects.

        self.proj_root_directory = str(args[0])
        self.compiler_args = str(args[1])

        directory_already_indexed = True
        indexer_directory_full_path = os.path.join(self.proj_root_directory, self.indexer_db_name)
        if not os.path.exists(indexer_directory_full_path):
            directory_already_indexed = False

        self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))
        if not directory_already_indexed:
            logging.info("Starting to index whole directory '{0}' ... ".format(self.proj_root_directory))
            self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))
            self.db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol_type (id integer, name text, PRIMARY KEY(id))')
            self.db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol (filename text, usr text, line integer, column integer, type integer, PRIMARY KEY(filename, usr, line, column), FOREIGN KEY (type) REFERENCES symbol_type(id))')
            symbol_types = [(1, 'function'), (2, 'variable'), (3, 'user_defined_type'), (4, 'macro'),]
            self.db.cursor().executemany('INSERT INTO symbol_type VALUES (?, ?)', symbol_types)
            start = time.clock()
            cpp_file_list = []
            for dirpath, dirs, files in os.walk(self.proj_root_directory):
                for file in files:
                    name, extension = os.path.splitext(file)
                    if extension in ['.cpp', '.cc', '.cxx', '.c', '.h', '.hh', '.hpp']:
                        cpp_file_list.append(os.path.join(dirpath, file))

            #self.run_on_directory_impl(self.proj_root_directory, self.compiler_args, cpp_file_list)

            cpp_file_list_sliced = slice_it(cpp_file_list, len(cpp_file_list)/self.cpu_count)
            process_list = []
            for slice in cpp_file_list_sliced:
                p = multiprocessing.Process(target=self.run_on_directory_impl, args=(self.proj_root_directory, self.compiler_args, slice))
                process_list.append(p)
                p.daemon = False
                p.start()
            for p in process_list:
                p.join()
            # TODO how to count total CPU time, for all processes?

            self.db.commit()

            time_elapsed = time.clock() - start
            logging.info("Indexing {0} took {1}.".format(self.proj_root_directory, time_elapsed))
        else:
            logging.info("Directory '{0}' already indexed ... ".format(self.proj_root_directory))

        if self.callback:
            self.callback(id, args)

    def __drop_single_file(self, id, args):
        # TODO For each indexer table:
        #       1. Remove symbols defined from file to be dropped
        if self.callback:
            self.callback(id, args)

    def __drop_all(self, id, dummy = None):
        # TODO Drop data from all tables
        if self.callback:
            self.callback(id, dummy)

    def __go_to_definition(self, id, args):
        cursor = self.parser.get_definition(
            self.parser.parse(
                str(args[0]),
                str(args[0]),
                self.compiler_args,
                self.proj_root_directory
            ),
            int(args[1]), int(args[2])
        )
        if cursor:
            logging.info('Definition location {0}'.format(str(cursor.location)))
        else:
            logging.info('No definition found.')

        if self.callback:
            self.callback(id, cursor.location if cursor else None)

    def __find_all_references(self, id, args):
        start = time.clock()
        references = []
        tunit = self.parser.parse(str(args[0]), str(args[0]), self.compiler_args, self.proj_root_directory)
        if tunit:
            cursor = self.parser.map_source_location_to_cursor(tunit, int(args[1]), int(args[2]))
            if cursor:
                logging.info("Finding all references of cursor [{0}, {1}]: {2}. name = {3}".format(cursor.location.line, cursor.location.column, tunit.spelling, cursor.displayname))
                usr = cursor.referenced.get_usr() if cursor.referenced else cursor.get_usr()
                ast_node_id = self.parser.get_ast_node_id(cursor)
                if ast_node_id in [ASTNodeId.getFunctionId(), ASTNodeId.getMethodId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getClassId(), ASTNodeId.getStructId(), ASTNodeId.getEnumId(), ASTNodeId.getEnumValueId(), ASTNodeId.getUnionId(), ASTNodeId.getTypedefId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getLocalVariableId(), ASTNodeId.getFunctionParameterId(), ASTNodeId.getFieldId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getMacroDefinitionId(), ASTNodeId.getMacroInstantiationId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                else:
                    query_result = None

                if query_result:
                    for row in query_result:
                        references.append((row[0], row[1], row[2], row[3]))
                        logging.debug('row: ' + str(row))

        logging.info('Found references: ' + str(references))
        time_elapsed = time.clock() - start
        logging.info("Find all references operation took {0}.".format(time_elapsed))

        if self.callback:
            self.callback(id, references)

    def __index_single_file(self, proj_root_directory, contents_filename, original_filename, compiler_args, db):
        def visitor(ast_node, ast_parent_node, parser):
            if (ast_node.location.file and ast_node.location.file.name == tunit.spelling):  # we are not interested in symbols which got into this TU via includes
                id = parser.get_ast_node_id(ast_node)
                usr = ast_node.referenced.get_usr() if ast_node.referenced else ast_node.get_usr()
                line = int(parser.get_ast_node_line(ast_node))
                column = int(parser.get_ast_node_column(ast_node))
                try:
                    if id in [ASTNodeId.getFunctionId(), ASTNodeId.getMethodId()]:
                        db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 1,))
                    elif id in [ASTNodeId.getClassId(), ASTNodeId.getStructId(), ASTNodeId.getEnumId(), ASTNodeId.getEnumValueId(), ASTNodeId.getUnionId(), ASTNodeId.getTypedefId()]:
                        db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 3,))
                    elif id in [ASTNodeId.getLocalVariableId(), ASTNodeId.getFunctionParameterId(), ASTNodeId.getFieldId()]:
                        db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 2,))
                    elif id in [ASTNodeId.getMacroDefinitionId(), ASTNodeId.getMacroInstantiationId()]:
                        db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 4,))
                    else:
                        pass
                except sqlite3.IntegrityError:
                    pass
                return ChildVisitResult.RECURSE.value  # If we are positioned in TU of interest, then we'll traverse through all descendants
            return ChildVisitResult.CONTINUE.value  # Otherwise, we'll skip to the next sibling

        logging.info("Indexing a file '{0}' ... ".format(original_filename))

        # TODO Indexing a single file does not guarantee us we'll have up-to-date AST's
        #       * Problem:
        #           * File we are indexing might be a header which is included in another translation unit
        #           * We would need a TU dependency tree to update influenced translation units as well

        # Index a single file
        start = time.clock()
        tunit = self.parser.parse(contents_filename, original_filename, compiler_args, proj_root_directory)
        if tunit:
            # TODO only if executed from index_single_file()
            #self.db.cursor().execute('DELETE FROM symbol WHERE filename=?', (tunit.spelling,))
            self.parser.traverse(tunit.cursor, self.parser, visitor)
            #self.db.commit() # TODO probably not needed, can be done from the outside
        time_elapsed = time.clock() - start
        logging.info("Indexing {0} took {1}.".format(original_filename, time_elapsed))

    def run_on_directory_impl(self, proj_root_directory, compiler_args, filename_list):
        db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))
        for filename in filename_list:
            if filename:
                self.__index_single_file(proj_root_directory, filename, filename, compiler_args, db)

