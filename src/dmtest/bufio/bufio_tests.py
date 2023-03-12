import dmtest.device_mapper.dev as dmdev
import dmtest.device_mapper.table as table
import dmtest.device_mapper.targets as targets
import dmtest.units as units
import dmtest.utils as utils
import enum
import logging as log
import mmap
import os
import random
import struct
import threading

from contextlib import contextmanager


class Instructions(enum.IntEnum):
    I_JMP = 0
    I_BNZ = 1
    I_BZ = 2
    I_HALT = 3
    I_LIT = 4
    I_SUB = 5
    I_ADD = 6
    I_DOWN_READ = 7
    I_UP_READ = 8
    I_DOWN_WRITE = 9
    I_UP_WRITE = 10
    I_INIT_BARRIER = 11
    I_WAIT_BARRIER = 12
    I_NEW_BUF = 13
    I_READ_BUF = 14
    I_GET_BUF = 15
    I_PUT_BUF = 16
    I_MARK_DIRTY = 17
    I_WRITE_ASYNC = 18
    I_WRITE_SYNC = 19
    I_FLUSH = 20
    I_FORGET = 21
    I_FORGET_RANGE = 22
    I_LOOP = 23
    I_STAMP = 24
    I_VERIFY = 25
    I_CHECKPOINT = 26


class BufioProgram:
    def __init__(self):
        self._bytes = b""
        self._labels = {}
        self._reg_alloc = 0

    def compile(self):
        return self._bytes[:]

    def alloc_reg(self):
        reg = self._reg_alloc
        self._reg_alloc += 1
        return reg

    def label(self, name):
        self._labels[name] = len(self._bytes)

    def label_to_addr(self, name):
        if name not in self._labels:
            raise ValueError("No such label '{}'", name)
        return self._labels[name]

    def jmp(self, name):
        addr = self.label_to_addr(name)
        self._bytes += struct.pack("=BH", Instructions.I_JMP, addr)

    def bnz(self, addr, reg):
        self._bytes += struct.pack("=BHB", Instructions.I_BNZ, addr, reg)

    def bz(self, addr, reg):
        self._bytes += struct.pack("=BHB", Instructions.I_BZ, addr, reg)

    def halt(self):
        self._bytes += struct.pack("=B", Instructions.I_HALT)

    def lit(self, val, reg):
        self._bytes += struct.pack("=BIB", Instructions.I_LIT, val, reg)

    def sub(self, reg1, reg2):
        self._bytes += struct.pack("=BBB", Instructions.I_SUB, reg1, reg2)

    def add(self, reg1, reg2):
        self._bytes += struct.pack("=BBB", Instructions.I_ADD, reg1, reg2)

    def down_read(self, lock):
        self._bytes += struct.pack("=BB", Instructions.I_DOWN_READ, lock)

    def up_read(self, lock):
        self._bytes += struct.pack("=BB", Instructions.I_UP_READ, lock)

    def down_write(self, lock):
        self._bytes += struct.pack("=BB", Instructions.I_DOWN_WRITE, lock)

    def up_write(self, lock):
        self._bytes += struct.pack("=BB", Instructions.I_UP_READ, lock)

    def init_barrier(self):
        pass

    def wait_barrier(self):
        pass

    def new_buf(self, block_reg, dest_reg):
        self._bytes += struct.pack("=BBB", Instructions.I_NEW_BUF, block_reg, dest_reg)

    def read_buf(self, block_reg, dest_reg):
        self._bytes += struct.pack("=BBB", Instructions.I_READ_BUF, block_reg, dest_reg)

    def get_buf(self, block_reg, dest_reg):
        self._bytes += struct.pack("=BBB", Instructions.I_GET_BUF, block_reg, dest_reg)

    def put_buf(self, reg):
        self._bytes += struct.pack("=BB", Instructions.I_PUT_BUF, reg)

    def mark_dirty(self, reg):
        self._bytes += struct.pack("=BB", Instructions.I_MARK_DIRTY, reg)

    def write_async(self):
        self._bytes += struct.pack("=B", Instructions.I_WRITE_ASYNC)

    def write_sync(self):
        self._bytes += struct.pack("=B", Instructions.I_WRITE_SYNC)

    def flush(self):
        self._bytes += struct.pack("=B", Instructions.I_FLUSH)

    def forget(self, block):
        self._bytes += struct.pack("=BI", Instructions.I_FORGET, block)

    def forget_range(self, block, len):
        self._bytes += struct.pack("=BII", Instructions.I_FORGET_RANGE, block, len)

    def loop(self, name, reg):
        addr = self.label_to_addr(name)
        self._bytes += struct.pack("=BHB", Instructions.I_LOOP, addr, reg)

    def stamp(self, buf_reg, pattern_reg):
        self._bytes += struct.pack("=BBB", Instructions.I_STAMP, buf_reg, pattern_reg)

    def verify(self, buf_reg, pattern_reg):
        self._bytes += struct.pack("=BBB", Instructions.I_VERIFY, buf_reg, pattern_reg)

    def checkpoint(self, reg):
        self._bytes += struct.pack("=BB", Instructions.I_CHECKPOINT, reg)


def exec_program(dev, program):
    bytes = program.compile()
    if len(bytes) > 4096:
        raise ValueError("buffer is too large")

    fd = os.open(dev.path, os.O_DIRECT | os.O_WRONLY)
    try:
        # Map a single page of memory to the file
        page_size = os.sysconf("SC_PAGE_SIZE")
        with mmap.mmap(-1, page_size) as mem:
            mem.write(bytes)
            os.write(fd, mem)
    finally:
        os.close(fd)


class Code:
    def __init__(self, thread_set):
        self._thread_set = thread_set
        self._code = BufioProgram()

    def __enter__(self):
        return self._code

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            return

        self._code.halt()
        self._thread_set.add_thread(self._code)


class ThreadSet:
    def __init__(self, dev):
        self._dev = dev
        self._programs = []

    def program(self):
        return Code(self)

    def add_thread(self, code):
        self._programs.append(code)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            return

        threads = []

        for code in self._programs:
            tid = threading.Thread(target=exec_program, args=(self._dev, code))
            threads.append(tid)

        for tid in threads:
            tid.start()

        for tid in threads:
            tid.join()


# Activate bufio test device and create a thread set
@contextmanager
def bufio_threads(data_dev):
    data_size = utils.dev_size(data_dev)
    t = table.Table(targets.BufioTestTarget(data_size, data_dev))
    with dmdev.dev(t) as dev:
        with ThreadSet(dev) as thread_set:
            yield thread_set


# -----------------------------------------------


def t_create(fix):
    with bufio_threads(fix.cfg["data_dev"]) as thread_set:
        pass


def t_empty_program(fix):
    with bufio_threads(fix.cfg["data_dev"]) as thread_set:
        with thread_set.program() as p:
            pass


def do_new_buf(p, base):
    block = p.alloc_reg()
    increment = p.alloc_reg()
    loop_counter = p.alloc_reg()
    buf = p.alloc_reg()

    p.lit(base, block)
    p.lit(1, increment)
    p.lit(1024, loop_counter)

    p.label("loop")
    p.new_buf(block, buf)
    p.put_buf(buf)
    p.add(block, increment)
    p.loop("loop", loop_counter)


def t_new_buf(fix):
    nr_threads = 16
    nr_gets = 1024

    with bufio_threads(fix.cfg["data_dev"]) as thread_set:
        for t in range(nr_threads):
            with thread_set.program() as p:
                do_new_buf(p, t * nr_gets)


def t_stamper(fix):
    with bufio_threads(fix.cfg["data_dev"]) as thread_set:
        with thread_set.program() as p:
            block = p.alloc_reg()
            increment = p.alloc_reg()
            loop_counter = p.alloc_reg()
            buf = p.alloc_reg()
            pattern = p.alloc_reg()

            p.lit(0, block)
            p.lit(1, increment)
            p.lit(1024, loop_counter)
            p.lit(random.randint(0, 1024), pattern)

            p.label("loop")

            # stamp
            p.new_buf(block, buf)
            p.stamp(buf, pattern)
            p.mark_dirty(buf)
            p.put_buf(buf)

            # write
            p.write_sync()
            p.forget(block)

            # re-read and verify
            p.read_buf(block, buf)
            p.verify(buf, pattern)
            p.put_buf(buf)

            p.add(block, increment)
            p.add(pattern, increment)
            p.loop("loop", loop_counter)


def do_stamper(p, base):
    block = p.alloc_reg()
    increment = p.alloc_reg()
    loop_counter = p.alloc_reg()
    buf = p.alloc_reg()
    pattern = p.alloc_reg()

    p.lit(base, block)
    p.lit(1, increment)
    p.lit(1024, loop_counter)
    p.lit(random.randint(0, 1024), pattern)

    p.label("loop")

    # stamp
    p.new_buf(block, buf)
    p.stamp(buf, pattern)
    p.mark_dirty(buf)
    p.put_buf(buf)

    # write
    p.write_sync()
    p.forget(block)

    # re-read and verify
    p.read_buf(block, buf)
    p.verify(buf, pattern)
    p.put_buf(buf)

    p.add(block, increment)
    p.add(pattern, increment)
    p.loop("loop", loop_counter)


def t_many_stampers(fix):
    nr_threads = 16
    nr_gets = 1024

    with bufio_threads(fix.cfg["data_dev"]) as thread_set:
        for t in range(nr_threads):
            with thread_set.program() as p:
                do_stamper(p, t * nr_gets)


# Mainly here as a benchmark
def t_writeback_many(fix):
    data_dev = fix.cfg["data_dev"]
    nr_blocks = units.gig(8) // 8

    with bufio_threads(data_dev) as thread_set:
        with thread_set.program() as p:
            block = p.alloc_reg()
            increment = p.alloc_reg()
            loop_counter = p.alloc_reg()
            buf = p.alloc_reg()

            p.lit(0, block)
            p.lit(1, increment)

            p.lit(nr_blocks, loop_counter)
            p.checkpoint(loop_counter)

            p.label("loop")

            p.new_buf(block, buf)
            p.mark_dirty(buf)
            p.put_buf(buf)

            # loop
            p.add(block, increment)
            p.loop("loop", loop_counter)

            # write
            p.checkpoint(loop_counter)
            p.write_sync()
            p.checkpoint(loop_counter)


def register(tests):
    tests.register("/bufio/create", t_create)
    tests.register("/bufio/empty-program", t_empty_program)
    tests.register("/bufio/new-buf", t_new_buf)
    tests.register("/bufio/stamper", t_stamper)
    tests.register("/bufio/many-stampers", t_many_stampers)
    tests.register("/bufio/writeback-many", t_writeback_many)
