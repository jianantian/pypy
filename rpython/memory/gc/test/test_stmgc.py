import py
from rpython.rlib.rarithmetic import r_uint
from rpython.rtyper.lltypesystem import lltype, llmemory, llarena, llgroup, rffi
from rpython.memory.gc.stmgc import StmGC, WORD, REV_INITIAL
from rpython.memory.gc.stmgc import GCFLAG_GLOBAL, GCFLAG_NOT_WRITTEN
from rpython.memory.gc.stmgc import GCFLAG_POSSIBLY_OUTDATED
from rpython.memory.gc.stmgc import GCFLAG_LOCAL_COPY, GCFLAG_VISITED
from rpython.memory.gc.stmgc import GCFLAG_HASH_FIELD
from rpython.memory.gc.stmgc import hdr_revision, set_hdr_revision
from rpython.memory.support import mangle_hash


S = lltype.GcStruct('S', ('a', lltype.Signed), ('b', lltype.Signed),
                         ('c', lltype.Signed))
ofs_a = llmemory.offsetof(S, 'a')

SR = lltype.GcForwardReference()
SR.become(lltype.GcStruct('SR', ('s1', lltype.Ptr(S)),
                                ('sr2', lltype.Ptr(SR)),
                                ('sr3', lltype.Ptr(SR))))

WR = lltype.GcStruct('WeakRef', ('wadr', llmemory.Address))
SWR = lltype.GcStruct('SWR', ('wr', lltype.Ptr(WR)))


class FakeStmOperations:
    # The point of this class is to make sure about the distinction between
    # RPython code in the GC versus C code in translator/stm/src_stm.  This
    # class contains a fake implementation of what should be in C.  So almost
    # any use of 'self._gc' is wrong here: it's stmgc.py that should call
    # et.c, and not the other way around.

    CALLBACK_ENUM = lltype.Ptr(lltype.FuncType([llmemory.Address] * 3,
                                               lltype.Void))
    DUPLICATE = lltype.Ptr(lltype.FuncType([llmemory.Address],
                                           llmemory.Address))

    threadnum = 0          # 0 = main thread; 1,2,3... = transactional threads

    def descriptor_init(self):
        self._in_transaction = False

    def begin_inevitable_transaction(self):
        assert self._in_transaction is False
        self._in_transaction = True

    def commit_transaction(self):
        assert self._in_transaction is True
        self._in_transaction = False

    def in_transaction(self):
        return self._in_transaction

    def set_tls(self, tls):
        assert lltype.typeOf(tls) == llmemory.Address
        assert tls
        if self.threadnum == 0:
            assert not hasattr(self, '_tls_dict')
            self._tls_dict = {0: tls}
            self._tldicts = {0: []}
            self._transactional_copies = []
        else:
            self._tls_dict[self.threadnum] = tls
            self._tldicts[self.threadnum] = []

    def get_tls(self):
        return self._tls_dict[self.threadnum]

    def del_tls(self):
        del self._tls_dict[self.threadnum]
        del self._tldicts[self.threadnum]

    def get_tldict(self):
        return self._tldicts[self.threadnum]

    def tldict_lookup(self, obj):
        assert lltype.typeOf(obj) == llmemory.Address
        assert obj
        for key, value in self.get_tldict():
            if obj == key:
                return value
        else:
            return llmemory.NULL

    def tldict_add(self, obj, localobj):
        assert lltype.typeOf(obj) == llmemory.Address
        assert lltype.typeOf(localobj) == llmemory.Address
        assert obj
        assert localobj
        tldict = self.get_tldict()
        for key, _ in tldict:
            assert obj != key
        tldict.append((obj, localobj))

    def tldict_enum(self):
        from rpython.memory.gc.stmtls import StmGCTLS
        callback = StmGCTLS._stm_enum_callback
        tls = self.get_tls()
        for key, value in self.get_tldict():
            assert (llmemory.cast_int_to_adr(self._gc.header(value).revision)
                    == key)
            callback(tls, value)


def fake_get_size(obj):
    TYPE = obj.ptr._TYPE.TO
    if isinstance(TYPE, lltype.GcStruct):
        return llmemory.sizeof(TYPE)
    else:
        assert 0

def fake_trace(obj, callback, arg):
    TYPE = obj.ptr._TYPE.TO
    if TYPE == S:
        ofslist = []     # no pointers in S
    elif TYPE == SR:
        ofslist = [llmemory.offsetof(SR, 's1'),
                   llmemory.offsetof(SR, 'sr2'),
                   llmemory.offsetof(SR, 'sr3')]
    elif TYPE == WR:
        ofslist = []
    elif TYPE == SWR:
        ofslist = [llmemory.offsetof(SWR, 'wr')]
    else:
        assert 0
    for ofs in ofslist:
        addr = obj + ofs
        if addr.address[0]:
            callback(addr, arg)

def fake_weakpointer_offset(tid):
    if tid == 124:
        return llmemory.offsetof(WR, 'wadr')
    else:
        return -1

class FakeRootWalker:
    def walk_current_stack_roots(self, *args):
        pass     # no stack roots in this test file
    def walk_current_nongc_roots(self, *args):
        pass     # no nongc roots in this test file


class StmGCTests:
    GCClass = StmGC

    def setup_method(self, meth):
        from pypy.config.pypyoption import get_pypy_config
        config = get_pypy_config(translating=True).translation
        self.gc = self.GCClass(config, FakeStmOperations(),
                               translated_to_c=False)
        self.gc.stm_operations._gc = self.gc
        self.gc.DEBUG = True
        self.gc.get_size = fake_get_size
        self.gc.trace = fake_trace
        self.gc.weakpointer_offset = fake_weakpointer_offset
        self.gc.root_walker = FakeRootWalker()
        self.gc.setup()

    def teardown_method(self, meth):
        if not hasattr(self, 'gc'):
            return
        for key in self.gc.stm_operations._tls_dict.keys():
            if key != 0:
                self.gc.stm_operations.threadnum = key
                self.gc.teardown_thread()
        self.gc.stm_operations.threadnum = 0

    # ----------
    # test helpers
    def malloc(self, STRUCT, weakref=False, globl=False):
        size = llarena.round_up_for_allocation(llmemory.sizeof(STRUCT))
        tid = lltype.cast_primitive(llgroup.HALFWORD, 123 + weakref)
        if globl:
            totalsize = self.gc.gcheaderbuilder.size_gc_header + size
            adr1 = llarena.arena_malloc(llmemory.raw_malloc_usage(totalsize),
                                        1)
            llarena.arena_reserve(adr1, totalsize)
            addr = adr1 + self.gc.gcheaderbuilder.size_gc_header
            self.gc.header(addr).tid = self.gc.combine(
                tid, GCFLAG_GLOBAL | GCFLAG_NOT_WRITTEN)
            self.gc.header(addr).revision = REV_INITIAL
            realobj = llmemory.cast_adr_to_ptr(addr, lltype.Ptr(STRUCT))
        else:
            gcref = self.gc.malloc_fixedsize_clear(tid, size,
                                                   contains_weakptr=weakref)
            realobj = lltype.cast_opaque_ptr(lltype.Ptr(STRUCT), gcref)
        return realobj
    def settldict(self, globl, locl):
        self.gc.stm_operations.tldict_add(llmemory.cast_ptr_to_adr(globl),
                                          llmemory.cast_ptr_to_adr(locl))
    def select_thread(self, threadnum):
        self.gc.stm_operations.threadnum = threadnum
        if threadnum not in self.gc.stm_operations._tls_dict:
            self.gc.setup_thread()
            self.gc.start_transaction()
    def gcsize(self, S):
        return (llmemory.raw_malloc_usage(llmemory.sizeof(self.gc.HDR)) +
                llmemory.raw_malloc_usage(llmemory.sizeof(S)))
    def checkflags(self, obj, globl='?', localcopy='?', version='?'):
        if lltype.typeOf(obj) != llmemory.Address:
            obj = llmemory.cast_ptr_to_adr(obj)
        hdr = self.gc.header(obj)
        if globl != '?':
            assert (hdr.tid & GCFLAG_GLOBAL != 0) == globl
        if localcopy != '?':
            assert (hdr.tid & GCFLAG_LOCAL_COPY != 0) == localcopy
        if version != '?':
            assert hdr.version == version

    def header(self, P):
        if lltype.typeOf(P) != llmemory.Address:
            P = llmemory.cast_ptr_to_adr(P)
        return self.gc.header(P)

    def set_hdr_revision(self, hdr, P):
        if lltype.typeOf(P) != llmemory.Address:
            P = llmemory.cast_ptr_to_adr(P)
        set_hdr_revision(hdr, P)

    def stm_readbarrier(self, P):
        P = llmemory.cast_ptr_to_adr(P)
        hdr = self.header(P)
        if hdr.tid & GCFLAG_GLOBAL == 0:
            # already a local object
            R = P
        else:
            R = self.stm_latest_global_rev(P)
            L = self.gc.stm_operations.tldict_lookup(R)
            if hdr.tid & GCFLAG_POSSIBLY_OUTDATED == 0:
                assert not L
            elif L:
                return L.ptr
        return R.ptr

    def stm_latest_global_rev(self, G):
        hdr = self.gc.header(G)
        assert hdr.tid & GCFLAG_GLOBAL != 0
        while hdr.revision != REV_INITIAL:
            xxx
        return G

    def stm_writebarrier(self, P):
        P = llmemory.cast_ptr_to_adr(P)
        hdr = self.header(P)
        if hdr.tid & GCFLAG_NOT_WRITTEN == 0:
            # already a local, written-to object
            assert hdr.tid & GCFLAG_GLOBAL == 0
            assert hdr.tid & GCFLAG_POSSIBLY_OUTDATED == 0
            W = P
        else:
            # slow case of the write barrier
            if hdr.tid & GCFLAG_GLOBAL == 0:
                W = P
                R = hdr_revision(hdr)
            else:
                R = P
                W = self.stm_localize(R)
            self.gc.header(W).tid &= ~GCFLAG_NOT_WRITTEN
            self.gc.header(R).tid |= GCFLAG_POSSIBLY_OUTDATED
        return W.ptr

    def stm_localize(self, R):
        L = self.gc.stm_operations.tldict_lookup(R)
        if L:
            assert self.gc.header(R).tid & GCFLAG_POSSIBLY_OUTDATED
        else:
            L = self.gc._stm_duplicate(R)
            hdr = self.gc.header(L)
            assert hdr.tid & GCFLAG_GLOBAL == 0
            assert hdr.tid & GCFLAG_POSSIBLY_OUTDATED == 0
            assert hdr.tid & GCFLAG_LOCAL_COPY
            assert hdr.tid & GCFLAG_NOT_WRITTEN
            set_hdr_revision(hdr, R)     # back-reference to the original
            self.gc.stm_operations.tldict_add(R, L)
            self.gc.stm_operations._transactional_copies.append((R.ptr, L.ptr))
        return L

    def do_local_collection(self):
        self.gc.stop_transaction()
        tldict = self.gc.stm_operations.get_tldict()
        for obj, localobj in tldict:
            hdr = self.gc.header(obj)
            localhdr = self.gc.header(localobj)
            assert localhdr.tid & GCFLAG_GLOBAL == 0
            assert localhdr.tid & GCFLAG_LOCAL_COPY
            assert localhdr.tid & GCFLAG_POSSIBLY_OUTDATED == 0
            localhdr.tid |= GCFLAG_GLOBAL | GCFLAG_NOT_WRITTEN
            localhdr.tid &= ~GCFLAG_LOCAL_COPY
            assert localhdr.revision.adr == obj
            localhdr.revision = r_uint(43)
            assert hdr.tid & GCFLAG_GLOBAL
            assert hdr.tid & GCFLAG_NOT_WRITTEN
            assert hdr.tid & GCFLAG_POSSIBLY_OUTDATED
            hdr.revision = llmemory.cast_adr_to_uint_symbolic(localobj)
        del tldict[:]
        self.gc.start_transaction()


class TestBasic(StmGCTests):

    def test_gc_creation_works(self):
        pass

    def test_allocate_bump_pointer(self):
        tls = self.gc.get_tls()
        a3 = tls.allocate_bump_pointer(3)
        a4 = tls.allocate_bump_pointer(4)
        a5 = tls.allocate_bump_pointer(5)
        a6 = tls.allocate_bump_pointer(6)
        assert a4 - a3 == 3
        assert a5 - a4 == 4
        assert a6 - a5 == 5

    def test_malloc_fixedsize_clear(self):
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        s = lltype.cast_opaque_ptr(lltype.Ptr(S), gcref)
        assert s.a == 0
        assert s.b == 0
        gcref2 = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        assert gcref2 != gcref

    def test_malloc_main_vs_thread(self):
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        obj = llmemory.cast_ptr_to_adr(gcref)
        assert self.gc.header(obj).tid & GCFLAG_GLOBAL == 0
        #
        self.select_thread(1)
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        obj = llmemory.cast_ptr_to_adr(gcref)
        assert self.gc.header(obj).tid & GCFLAG_GLOBAL == 0

    def test_write_barrier_exists(self):
        self.select_thread(1)
        t = self.malloc(S)
        obj = self.stm_writebarrier(t)     # local object
        assert obj == t
        #
        self.select_thread(0)
        s = self.malloc(S, globl=True)
        #
        self.select_thread(1)
        assert self.header(s).tid & GCFLAG_GLOBAL != 0
        assert self.header(t).tid & GCFLAG_GLOBAL == 0
        self.header(s).tid |= GCFLAG_POSSIBLY_OUTDATED
        self.header(t).tid |= GCFLAG_LOCAL_COPY | GCFLAG_VISITED
        self.set_hdr_revision(self.header(t), s)
        self.settldict(s, t)
        obj = self.stm_writebarrier(s)     # global copied object
        assert obj == t
        assert self.gc.stm_operations._transactional_copies == []

    def test_write_barrier_new(self):
        self.select_thread(0)
        s = self.malloc(S, globl=True)     # global object, not copied so far
        s.a = 12
        s.b = 34
        #
        self.select_thread(1)
        t = self.stm_writebarrier(s)
        assert t != s
        assert t.a == 12
        assert t.b == 34
        assert self.gc.stm_operations._transactional_copies == [(s, t)]
        #
        u = self.stm_writebarrier(s)          # again
        assert u == t
        #
        u = self.stm_writebarrier(u)          # local object
        assert u == t

    def test_write_barrier_main_thread(self):
        t = self.malloc(S, globl=False)
        self.checkflags(t, globl=False, localcopy=False)
        obj = self.stm_writebarrier(t)        # main thread, but not global
        assert obj == t
        self.checkflags(obj, globl=False, localcopy=False)

    def test_random_gc_usage(self):
        py.test.skip("XXX")
        import random
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        #
        sr2 = {}    # {obj._obj: obj._obj} following the 'sr2' attribute
        sr3 = {}    # {obj._obj: obj._obj} following the 'sr3' attribute
        #
        def reachable(source_objects):
            pending = list(source_objects)
            found = set(obj._obj for obj in pending)
            for x in pending:
                x = self.stm_readbarrier(x)
                for name in ('sr2', 'sr3'):
                    obj = getattr(x, name)
                    if obj and obj._obj not in found:
                        found.add(obj._obj)
                        pending.append(obj)
            return found
        #
        def shape_of_reachable(source_object, can_be_indirect=True):
            shape = []
            pending = [source_object]
            found = {source_object._obj: 0}
            for x in pending:
                x_orig = x
                x = self.stm_readbarrier(x)
                if not can_be_indirect:
                    assert x == x_orig
                for name in ('sr2', 'sr3'):
                    obj = getattr(x, name)
                    if not obj:
                        shape.append(None)
                    else:
                        if obj._obj not in found:
                            found[obj._obj] = len(found)
                            pending.append(obj)
                        shape.append(found[obj._obj])
            return shape
        #
        prebuilt = [self.malloc(SR, globl=True) for i in range(15)]
        globals = set(obj._obj for obj in prebuilt)
        root_objects = prebuilt[:]
        all_objects = root_objects[:]
        NO_OBJECT = lltype.nullptr(SR)
        #
        for iteration in range(3):
            # add 6 freshly malloced objects from the nursery
            new_objects = [self.malloc(SR, globl=False) for i in range(6)]
            set_new_objects = set(obj._obj for obj in new_objects)
            all_objects = all_objects + new_objects
            set_all_objects = set(obj._obj for obj in all_objects)
            #
            # pick 4 random objects to be stack roots
            fromstack = random.sample(all_objects, 4)
            root_objects = prebuilt + fromstack
            #
            # randomly add or remove connections between objects, until they
            # are all reachable from root_objects
            for trying in xrange(200):
                missing_objects = set_all_objects - reachable(root_objects)
                if not missing_objects:
                    break
                srcobj = random.choice(all_objects)
                # give a higher chance to 'missing_objects', but also
                # allows other objects
                missing_objects = [obj._as_ptr() for obj in missing_objects]
                missing_objects.append(NO_OBJECT)
                missing_objects *= 2
                missing_objects.extend(all_objects)
                dstobj = random.choice(missing_objects)
                name = random.choice(('sr2', 'sr3'))
                obj2 = self.stm_writebarrier(srcobj)
                setattr(obj2, name, dstobj)
            #
            # Record the shape of the graph of reachable objects
            shapes = [shape_of_reachable(obj) for obj in root_objects]
            #
            # Do a local end-of-transaction collection
            for p in fromstack:
                self.gc.root_walker.push(p)
            self.do_local_collection()
            #
            # Reload 'fromstack', which may have moved, and compare the shape
            # of the graph of reachable objects now
            for i in range(len(fromstack)-1, -1, -1):
                fromstack[i] = self.gc.root_walker.pop()
            root_objects = prebuilt + fromstack
            shapes2 = [shape_of_reachable(obj, can_be_indirect=False)
                       for obj in root_objects]
            assert shapes == shapes2
            #
            # Reset the list of all objects for the next iteration
            all_objects = [obj._as_ptr() for obj in reachable(root_objects)]
            #
            # Check the GLOBAL flag, and check that the objects really survived
            for obj in all_objects:
                self.checkflags(obj, obj._obj in globals, '?')
                localobj = self.gc.stm_operations.tldict_lookup(
                    llmemory.cast_ptr_to_adr(obj))
                if localobj:
                    self.checkflags(localobj, False, False)
            print 'Iteration %d finished' % iteration

    def test_relocalize_objects_after_transaction_break(self):
        py.test.skip("XXX")
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        #
        tr1 = self.malloc(SR, globl=True)   # three prebuilt objects
        tr2 = self.malloc(SR, globl=True)
        tr3 = self.malloc(SR, globl=True)
        tr1.sr2 = tr2
        self.gc.root_walker.push(tr1)
        sr1 = self.stm_writebarrier(tr1)
        assert sr1 != tr1
        sr2 = self.stm_writebarrier(tr2)
        assert sr2 != tr2
        sr3 = self.gc.stm_writebarrier(tr3)
        assert sr3 != tr3
        self.checkflags(sr1, False, True)    # sr1 is local
        self.checkflags(sr2, False, True)    # sr2 is local
        self.checkflags(sr3, False, True)    # sr3 is local
        #
        self.gc.stop_transaction()
        self.gc.start_transaction()
        self.checkflags(tr1_adr, True, True)     # tr1 has become global again
        self.checkflags(tr2_adr, True, True)     # tr2 has become global again
        self.checkflags(tr3_adr, True, True)     # tr3 has become global again

    def test_obj_with_invalid_offset_after_transaction_stop(self):
        py.test.skip("XXX")
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        #
        tr1 = self.malloc(SR, globl=False)  # local
        self.checkflags(tr1_adr, False, False)    # check that it is local
        self.gc.root_walker.push(tr1)
        self.gc.stop_transaction()
        # now tr1 is stored in the shadowstack with an offset of 2 to mark
        # that it was local.
        py.test.raises(llarena.ArenaError, self.gc.root_walker.pop)

    def test_non_prebuilt_relocalize_after_transaction_break(self):
        py.test.skip("XXX")
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        #
        tr1 = self.malloc(SR, globl=False)  # local
        tr2 = self.malloc(SR, globl=False)  # local
        self.checkflags(tr1_adr, False, False)    # check that it is local
        self.checkflags(tr2_adr, False, False)    # check that it is local
        tr1.sr2 = tr2
        self.gc.root_walker.push(tr1)
        self.gc.stop_transaction()
        self.gc.start_transaction()
        # tr1 and tr2 moved out of the nursery: check that
        sr1 = self.gc.root_walker.pop()
        assert sr1._obj0 != tr1._obj0
        sr2 = sr1.sr2
        assert sr2 and sr2 != sr1 and not sr2.sr2
        assert sr2._obj0 != tr2._obj0
        sr1_adr = llmemory.cast_ptr_to_adr(sr1)
        sr2_adr = llmemory.cast_ptr_to_adr(sr2)
        self.checkflags(sr1_adr, False, True)     # sr1 is a WAS_COPIED local
        self.checkflags(sr2_adr, True, False)     # sr2 is a global

    def test_collect_from_main_thread_was_global_objects(self):
        py.test.skip("XXX")
        tr1 = self.malloc(SR, globl=True)  # a global prebuilt object
        sr2 = self.malloc(SR, globl=False) # sr2 is a local
        self.checkflags(sr2_adr, False, False)      # check that sr2 is a local
        sr1_adr = self.gc.stm_writebarrier(tr1_adr)
        assert sr1_adr != tr1_adr                   # sr1 is the local copy
        sr1 = llmemory.cast_adr_to_ptr(sr1_adr, lltype.Ptr(SR))
        sr1.sr2 = sr2
        self.gc.stop_transaction()
        self.checkflags(tr1_adr, True, True)       # tr1 is still global
        assert tr1.sr2 == lltype.nullptr(SR)   # the copying is left to C code
        tr2 = sr1.sr2                          # from sr1
        assert tr2
        assert tr2._obj0 != sr2._obj0
        tr2_adr = llmemory.cast_ptr_to_adr(tr2)
        self.checkflags(tr2_adr, True, False)      # tr2 is now global

    def test_commit_transaction_empty(self):
        self.select_thread(1)
        s = self.malloc(S)
        t = self.malloc(S)
        self.gc.stop_transaction()    # no roots
        self.gc.start_transaction()
        main_tls = self.gc.get_tls()
        assert main_tls.nursery_free == main_tls.nursery_start   # empty

    def test_commit_tldict_entry_with_global_references(self):
        py.test.skip("XXX")
        t  = self.malloc(S)
        tr = self.malloc(SR)
        tr.s1 = t
        self.select_thread(1)
        sr_adr = self.gc.stm_writebarrier(tr_adr)
        assert sr_adr != tr_adr
        s_adr = self.gc.stm_writebarrier(t_adr)
        assert s_adr != t_adr

    def test_commit_local_obj_with_global_references(self):
        py.test.skip("XXX")
        t  = self.malloc(S)
        tr = self.malloc(SR)
        tr.s1 = t
        self.select_thread(1)
        sr_adr = self.gc.stm_writebarrier(tr_adr)
        assert sr_adr != tr_adr
        sr = llmemory.cast_adr_to_ptr(sr_adr, lltype.Ptr(SR))
        sr2 = self.malloc(SR)
        sr.sr2 = sr2

    def test_commit_with_ref_to_local_copy(self):
        py.test.skip("XXX")
        tr = self.malloc(SR)
        sr_adr = self.gc.stm_writebarrier(tr_adr)
        assert sr_adr != tr_adr
        sr = llmemory.cast_adr_to_ptr(sr_adr, lltype.Ptr(SR))
        sr.sr2 = sr
        self.gc.stop_transaction()
        assert sr.sr2 == tr

    def test_do_get_size(self):
        py.test.skip("XXX")
        s1 = self.malloc(S)
        assert (repr(self.gc._stm_getsize(s1_adr)) ==
                repr(fake_get_size(s1_adr)))

    def test_id_of_global(self):
        py.test.skip("XXX")
        s = self.malloc(S)
        i = self.gc.id(s)
        assert i == llmemory.cast_adr_to_int(s_adr)

    def test_id_of_globallocal(self):
        py.test.skip("XXX")
        s = self.malloc(S)
        t_adr = self.gc.stm_writebarrier(s_adr)   # make a local copy
        assert t_adr != s_adr
        t = llmemory.cast_adr_to_ptr(t_adr, llmemory.GCREF)
        i = self.gc.id(t)
        assert i == llmemory.cast_adr_to_int(s_adr)
        assert i == self.gc.id(s)
        self.gc.stop_transaction()
        assert i == self.gc.id(s)

    def test_id_of_local_nonsurviving(self):
        py.test.skip("XXX")
        s = self.malloc(S, globl=False)
        i = self.gc.id(s)
        assert i != llmemory.cast_adr_to_int(s_adr)
        assert i == self.gc.id(s)
        self.gc.stop_transaction()

    def test_id_of_local_surviving(self):
        py.test.skip("XXX")
        sr1 = self.malloc(SR, globl=True)
        assert sr1.s1 == lltype.nullptr(S)
        assert sr1.sr2 == lltype.nullptr(SR)
        t2 = self.malloc(S, globl=False)
        t2.a = 423
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        assert tr1_adr != sr1_adr
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        i = self.gc.id(t2)
        assert i not in (llmemory.cast_adr_to_int(sr1_adr),
                         llmemory.cast_adr_to_int(t2_adr),
                         llmemory.cast_adr_to_int(tr1_adr))
        assert i == self.gc.id(t2)
        self.gc.stop_transaction()
        s2 = tr1.s1       # tr1 is a root, so not copied yet
        assert s2 and s2.a == 423 and s2._obj0 != t2._obj0
        assert self.gc.id(s2) == i

    def test_hash_of_global(self):
        py.test.skip("XXX")
        s = self.malloc(S)
        i = self.gc.identityhash(s)
        assert i == mangle_hash(llmemory.cast_adr_to_int(s_adr))
        self.gc.collect(0)
        assert self.gc.identityhash(s) == i

    def test_hash_of_globallocal(self):
        py.test.skip("XXX")
        s = self.malloc(S, globl=True)
        t_adr = self.stm_writebarrier(s_adr)   # make a local copy
        t = llmemory.cast_adr_to_ptr(t_adr, llmemory.GCREF)
        i = self.gc.identityhash(t)
        assert i == mangle_hash(llmemory.cast_adr_to_int(s_adr))
        assert i == self.gc.identityhash(s)
        self.gc.stop_transaction()
        assert i == self.gc.identityhash(s)

    def test_hash_of_local_nonsurviving(self):
        py.test.skip("XXX")
        s = self.malloc(S, globl=False)
        i = self.gc.identityhash(s)
        # XXX fix me
        #assert i != mangle_hash(llmemory.cast_adr_to_int(s_adr))
        assert i == self.gc.identityhash(s)
        self.gc.stop_transaction()

    def test_hash_of_local_surviving(self):
        py.test.skip("XXX")
        sr1 = self.malloc(SR, globl=True)
        t2 = self.malloc(S, globl=False)
        t2.a = 424
        tr1_adr = self.stm_writebarrier(sr1_adr)
        assert tr1_adr != sr1_adr
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        i = self.gc.identityhash(t2)
        assert i not in map(mangle_hash,
                        (llmemory.cast_adr_to_int(sr1_adr),
                         #llmemory.cast_adr_to_int(t2_adr),  XXX fix me
                         llmemory.cast_adr_to_int(tr1_adr)))
        assert i == self.gc.identityhash(t2)
        self.gc.stop_transaction()
        s2 = tr1.s1       # tr1 is a root, so not copied yet
        assert s2 and s2.a == 424 and s2._obj0 != t2._obj0
        assert self.gc.identityhash(s2) == i

    def test_weakref_to_global(self):
        py.test.skip("XXX")
        swr1 = self.malloc(SWR, globl=True)
        s2 = self.malloc(S, globl=True)
        wr1 = self.malloc(WR, globl=False, weakref=True)
        wr1.wadr = s2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        self.gc.stop_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2._obj0 != wr1._obj0
        assert wr2.wadr == s2_adr   # survives

    def test_weakref_to_local_dying(self):
        py.test.skip("XXX")
        swr1 = self.malloc(SWR, globl=True)
        t2   = self.malloc(S, globl=False)
        wr1  = self.malloc(WR, globl=False, weakref=True)
        wr1.wadr = t2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        self.gc.stop_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2._obj0 != wr1._obj0
        assert wr2.wadr == llmemory.NULL   # dies

    def test_weakref_to_local_surviving(self):
        py.test.skip("XXX")
        sr1  = self.malloc(SR, globl=True)
        swr1 = self.malloc(SWR, globl=True)
        t2   = self.malloc(S, globl=False)
        wr1  = self.malloc(WR, globl=False, weakref=True)
        wr1.wadr = t2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        t2.a = 4242
        self.gc.stop_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2._obj0 != wr1._obj0
        assert wr2.wadr and wr2.wadr.ptr._obj0 != t2_adr.ptr._obj0   # survives
        s2 = llmemory.cast_adr_to_ptr(wr2.wadr, lltype.Ptr(S))
        assert s2.a == 4242
        assert s2 == tr1.s1   # tr1 is a root, so not copied yet

    def test_weakref_to_local_in_main_thread(self):
        py.test.skip("XXX")
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        #
        sr1 = self.malloc(SR, globl=False)
        wr1 = self.malloc(WR, globl=False, weakref=True)
        wr1.wadr = sr1_adr
        #
        self.gc.root_walker.push(wr1)
        self.gc.collect(0)
        wr1 = self.gc.root_walker.pop()
        assert not wr1.wadr        # weakref to dead object
        #
        self.gc.root_walker.push(wr1)
        self.gc.collect(0)
        wr1bis = self.gc.root_walker.pop()
        assert wr1 == wr1bis
        assert not wr1.wadr

    def test_normalize_global_null(self):
        py.test.skip("XXX")
        a = self.gc.stm_normalize_global(llmemory.NULL)
        assert a == llmemory.NULL

    def test_normalize_global_already_global(self):
        py.test.skip("XXX")
        sr1 = self.malloc(SR)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr

    def test_normalize_global_purely_local(self):
        py.test.skip("XXX")
        self.select_thread(1)
        sr1 = self.malloc(SR)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr

    def test_normalize_global_local_copy(self):
        py.test.skip("XXX")
        sr1 = self.malloc(SR)
        self.select_thread(1)
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr
        a = self.gc.stm_normalize_global(tr1_adr)
        assert a == sr1_adr

    def test_prebuilt_nongc(self):
        from rpython.memory.gc.test import test_stmtls
        self.gc.root_walker = test_stmtls.FakeRootWalker()
        NONGC = lltype.Struct('NONGC', ('s', lltype.Ptr(S)))
        nongc = lltype.malloc(NONGC, immortal=True, flavor='raw')
        self.gc.root_walker.prebuilt_nongc = [(nongc, 's')]
        #
        s = self.malloc(S, globl=False)      # a local object
        nongc.s = s
        self.gc.collect(0)                      # keeps LOCAL
        s = nongc.s                             # reload, it moved
        s_adr = llmemory.cast_ptr_to_adr(s)
        self.checkflags(s_adr, False, False)    # check it survived; local
