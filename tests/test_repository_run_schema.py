"""Inactive repository-schema constraints, not a Core or Host acceptance story."""
from hashlib import sha256
import sqlite3
import unittest

from support.repository_scope import connection, insert, seed

class RepositoryRunSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db=connection()
        self.addCleanup(self.db.close)
        self.db.execute('BEGIN')
        seed(self.db,'A');seed(self.db,'B')
        self.db.commit()
    def test_reused_subject_material_and_profile_ids_are_separate(self):
        for run in ('A','B'):
            self.assertEqual(self.db.execute('SELECT count(*) FROM subjects WHERE run_id=?',(run,)).fetchone()[0],1)
            self.assertEqual(self.db.execute('SELECT body FROM material WHERE run_id=? AND id=?',(run,'profile')).fetchone()[0],b'original profile')
            insert(self.db,'work_queue',{'run_id':run,'subject':'parent:1','ready':1})
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
    def test_queues_and_gates_cannot_bind_a_foreign_only_subject(self):
        self.db.execute('BEGIN');seed(self.db,'C',subject='only-C');self.db.commit()
        for table,values in [('work_queue',{'ready':2}),('integration_queue',{'ready':2}),('gates',{'code':'operator_stopped'})]:
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                insert(self.db,table,{'run_id':'A','subject':'only-C',**values})
    def test_threads_cannot_bind_foreign_material_profiles(self):
        body=b'foreign-only'
        insert(self.db,'material',{'run_id':'B','id':'B-only-material','subject':'parent:1','purpose':'profile','digest':sha256(body).hexdigest(),'size':len(body),'body':body})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db,'profile_revisions',{'run_id':'A','id':2,'subject':'parent:1','material_id':'B-only-material','created_at':100.})
        insert(self.db,'profile_revisions',{'run_id':'B','id':2,'subject':'parent:1','material_id':'B-only-material','created_at':100.})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db,'threads',{'run_id':'A','id':'foreign-profile','subject':'parent:1','role':'development','profile_revision':2})
    def test_material_owned_subject_cannot_cross_run(self):
        self.db.execute('BEGIN');seed(self.db,'C',subject='only-C');self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db,'material',{'run_id':'A','id':'bad','subject':'only-C','purpose':'test','digest':'x','size':1,'body':b'x'})
    def test_all_foreign_relations_have_matching_run_scope(self):
        tables=[row[0] for row in self.db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        self.assertEqual(len(tables),39)
        self.assertIn("run_repairs", tables)
        for table in tables:
            columns={r[1] for r in self.db.execute('PRAGMA table_info('+table+')')}
            if table not in {'meta','runs'}:self.assertIn('run_id',columns)
            groups={}
            for row in self.db.execute('PRAGMA foreign_key_list('+table+')'):
                groups.setdefault(row[0],[]).append(row)
            for rows in groups.values():
                target=rows[0][2]
                with self.subTest(table=table,target=target):
                    if target=='runs':
                        self.assertTrue(any(r[3]=='run_id' and r[4]=='id' for r in rows))
                    else:
                        expected='id' if table=='runs' else 'run_id'
                        self.assertTrue(any(r[3]==expected and r[4]=='run_id' for r in rows),rows)
    def test_repair_relations_keep_reviews_checks_and_open_identity_in_the_same_run(self):
        self.db.execute('BEGIN');seed(self.db,'C',subject='only-C');self.db.commit()
        for run, attempt in (('A','A-review'), ('B','B-review')):
            insert(self.db,'attempts',{'run_id':run,'id':attempt,'subject':'parent:1','generation':1,
                'ordinal':1,'role':'review','purpose':'integration','window':1,'parent_revision':'r',
                'graph_revision':'g','revision':'r','base_sha':'a'*40,'completion_revision':'no-tickets',
                'context_id':'profile','status':'consumed'})
        insert(self.db,'checks',{'run_id':'B','id':'B-checks','subject':'parent:1','pr_number':7,
            'head_sha':'c'*40,'result':'none','material_id':'profile'})
        record = {'run_id':'A','id':'same-repair','subject':'parent:1','sequence':1,'generation':1,
            'state':'open','source_ref':'refs/heads/repair','target_ref':'refs/heads/run',
            'run_sha':'a'*40,'default_sha':'a'*40,'candidate_sha':'c'*40,'candidate_tree':'d'*40,
            'integrated_sha':'c'*40,'integrated_tree':'d'*40,'expected_tree':'d'*40,
            'review_attempt':'A-review','revision':'r','parent_revision':'r','graph_revision':'g',
            'completion_revision':'no-tickets'}
        for change in ({'subject':'only-C'}, {'review_attempt':'B-review'}, {'checks_id':'B-checks'}):
            with self.subTest(change=change), self.assertRaises(sqlite3.IntegrityError):
                insert(self.db,'run_repairs',dict(record,**change))
        insert(self.db,'run_repairs',record)
        insert(self.db,'run_repairs',dict(record,run_id='B',review_attempt='B-review',checks_id='B-checks'))
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db,'run_repairs',dict(record,id='second-open',sequence=2))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_foreign_opaque_attempt_is_not_a_valid_invocation_owner(self):
        insert(self.db,'attempts',{'run_id':'B','id':'B-only-attempt','subject':'parent:1','generation':1,
            'ordinal':1,'role':'development','purpose':'requirements','window':1,'parent_revision':'r','graph_revision':'g',
            'revision':'r','base_sha':'a'*40,'completion_revision':'no-tickets','context_id':'profile','status':'allocated'})
        for run in ('A','B'):
            insert(self.db,'threads',{'run_id':run,'id':'same-thread-label','subject':'parent:1','role':'development','profile_revision':1})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db,'invocations',{'run_id':'A','id':'bad','attempt':'B-only-attempt','thread':'same-thread-label','executor':'owner','epoch':1,'status':'running','allocated_at':100.,'deadline_seconds':60.,'deadline_at':160.})
    def test_scope_update_delete_and_rollback_do_not_touch_sibling(self):
        insert(self.db,'gates',{'run_id':'A','subject':'parent:1','code':'operator_stopped'})
        insert(self.db,'gates',{'run_id':'B','subject':'parent:1','code':'operator_stopped'})
        before=list(self.db.execute('SELECT run_id,subject,code FROM gates ORDER BY run_id'))
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM gates WHERE run_id=? AND subject=?',('A','parent:1'))
        self.db.execute('UPDATE subjects SET version=version+1 WHERE run_id=? AND id=?',('A','parent:1'))
        self.assertEqual(self.db.execute('SELECT version FROM subjects WHERE run_id=?',('B',)).fetchone()[0],1)
        self.db.rollback()
        self.assertEqual(list(self.db.execute('SELECT run_id,subject,code FROM gates ORDER BY run_id')),before)

    def test_current_attempt_pointer_cannot_name_another_runs_attempt(self):
        insert(self.db,'attempts',{'run_id':'B','id':'foreign-attempt','subject':'parent:1','generation':1,
            'ordinal':1,'role':'development','purpose':'requirements','window':1,'parent_revision':'r','graph_revision':'g',
            'revision':'r','base_sha':'a'*40,'completion_revision':'no-tickets','context_id':'profile','status':'allocated'})
        self.db.execute('BEGIN')
        try:
            self.db.execute('UPDATE subjects SET current_attempt=? WHERE run_id=? AND id=?',
                            ('foreign-attempt','A','parent:1'))
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.commit()
        finally:
            self.db.rollback()
        self.assertIsNone(self.db.execute('SELECT current_attempt FROM subjects WHERE run_id=?',('A',)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts WHERE run_id=?',('B',)).fetchone()[0],1)

    def test_final_action_cannot_bind_a_foreign_run(self):
        insert(self.db,'actions',{'run_id':'B','id':'foreign-approve','kind':'approve',
            'subject':'parent:1','payload_id':'profile','status':'pending'})
        self.db.execute('BEGIN')
        try:
            self.db.execute('UPDATE runs SET final_action=? WHERE id=?',('foreign-approve','A'))
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.commit()
        finally:
            self.db.rollback()
        self.assertIsNone(self.db.execute('SELECT final_action FROM runs WHERE id=?',('A',)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT status FROM actions WHERE run_id=?',('B',)).fetchone()[0],'pending')

    def test_relation_keys_do_not_repeat_the_run_column(self):
        tables=[row[0] for row in self.db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            for index in self.db.execute('PRAGMA index_list('+table+')'):
                columns=[row[2] for row in self.db.execute('PRAGMA index_info('+index[1]+')')]
                with self.subTest(table=table,index=index[1]):
                    self.assertEqual(len(columns),len(set(columns)))

if __name__=='__main__':unittest.main()
