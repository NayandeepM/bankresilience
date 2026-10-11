import {test} from 'node:test';
import assert from 'node:assert/strict';
import {experimentView,diagnosisFor} from './view.js';
test('active experiment never borrows the last diagnosis or selected history',()=>{
 const old={id:'old',experiment_type:'ledgerwriter-outage',diagnosis:{experiment_id:'old',experiment_type:'ledgerwriter-outage'}};
 const current={id:'new',experiment_type:'network-slowdown',diagnosis:null,telemetry:[]};
 const v=experimentView({active:true,experiment:current,last_result:old},[{id:'old',result:old}],'old');
 assert.equal(v,current);assert.equal(diagnosisFor(v),null);assert.deepEqual(v.telemetry,[]);
});
test('historical selection binds metrics and diagnosis to one ID',()=>{
 const saved={id:'a',experiment_type:'contacts-pause',telemetry:[1],diagnosis:{experiment_id:'a',experiment_type:'contacts-pause'}};
 assert.equal(experimentView({active:false,experiment:{id:'b'}},[{id:'a',result:saved}],'a'),saved);
 assert.equal(diagnosisFor(saved),saved.diagnosis);
});
test('wrong ID, type, legacy diagnosis and missing selection cannot leak',()=>{
 assert.equal(diagnosisFor({id:'a',experiment_type:'network-slowdown',diagnosis:{experiment_id:'b',experiment_type:'network-slowdown'}}),null);
 assert.equal(diagnosisFor({id:'a',experiment_type:'network-slowdown',diagnosis:{experiment_id:'a',experiment_type:'ledgerwriter-outage'}}),null);
 assert.equal(diagnosisFor({id:'a',diagnosis:{root_cause:'old'}}),null);
 assert.equal(experimentView({active:false,last_result:{id:'old'}},[],'missing'),null);
});
