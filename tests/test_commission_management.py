"""Exercise management commissioning's real JS protocol with a synthetic socket.

Node is optional for contributors. The disposable deployment also executes the
same client against the actual Socket.IO servers inside their pinned images.
"""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest


PATH = Path(__file__).resolve().parent.parent / "scripts" / "commission-apps.py"
SPEC = importlib.util.spec_from_file_location("commission_management", PATH)
commission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(commission)


# Every field and password in this protocol fixture is synthetic. Never import
# a real config, browser session, deployment secrets directory, or Docker state.
RUNNER = r'''
const fs=require("fs"),vm=require("vm");
const input=JSON.parse(fs.readFileSync(0,"utf8")), f=input.fixture;
const handlers=new Map(),calls=[],output=[];
let enrolled=!f.fresh,readyCalls=0;
function fire(event,...args){for(const fn of [...(handlers.get(event)||[])])fn(...args);}
const s={
 on(event,fn){if(!handlers.has(event))handlers.set(event,[]);handlers.get(event).push(fn);return s;},
 off(event,fn){handlers.set(event,(handlers.get(event)||[]).filter(x=>x!==fn));return s;},
 close(){},timeout(){return s;},
 emit(...args){
  const done=args.pop(),name=args[0];calls.push(name==="agent"?args[2]:name);
  setImmediate(()=>{
   if(name==="needSetup"){
    readyCalls++;
    if(f.lostReady&&readyCalls===1)return done(new Error("not-ready"));
    return done(null,f.invalidReady?{}:!enrolled);
   }
   if(name==="setup"){enrolled=true;return done(null,{ok:true});}
   if(name==="login")return done(null,{ok:enrolled&&!f.badLogin});
   if(name==="getMonitorList"){
    if(!f.missingMonitorEvent)fire("monitorList",{});
    return done(null,{ok:true});
   }
   if(name==="agent"&&args[2]==="requestStackList"){
    fire("agent","stackList",{ok:true,endpoint:"",stackList:f.missingStack?{}:{fixture:{name:"fixture",status:f.stopped?4:3}}});
    return done(null,{ok:true});
   }
   if(name==="agent"&&args[2]==="getStack")return done(null,{ok:true,stack:{name:args[3],isManagedByDockge:!f.foreign,status:0}});
   if(name==="agent"&&args[2]==="serviceStatusList")return done(null,{ok:true,serviceStatusList:f.stopped?{}:{fixture:"running"}});
   done(new Error("unexpected-event"));
  });return s;
 }
};
const mockedRequire=name=>name==="fs"?{readFileSync:()=>JSON.stringify({
 service:f.service||"uptime-kuma",url:"http://127.0.0.1:3001",
 username:"synthetic-admin",password:"synthetic-fixture-only",projects:["fixture"]
})}:name==="socket.io-client"?{io:()=>{setImmediate(()=>{
 if(f.fresh&&f.service==="dockge")fire("setup");fire("connect");
});return s;}}:require(name);
vm.runInNewContext(input.source,{
 require:mockedRequire,console:{log:value=>output.push(value)},
 process:{exit:code=>{process.stdout.write(JSON.stringify({code,calls,output}));process.exit(0);}},
 setTimeout:(fn,ms)=>setTimeout(fn,ms===45000?1000:Math.min(ms,30)),clearTimeout
});
'''


@unittest.skipUnless(shutil.which("node"), "Node is optional; live image client tested in disposable Linux commissioning")
class ManagementSocketTests(unittest.TestCase):
    def execute(self, **fixture):
        result = subprocess.run(
            [shutil.which("node"), "-e", RUNNER],
            input=json.dumps({"source": commission.SOCKET_SCRIPT, "fixture": fixture}),
            # The protocol fixture has its own one-second watchdog. Allow a
            # separate process-start budget for contended hosted CI runners.
            capture_output=True, text=True, timeout=20, check=True,
        )
        return json.loads(result.stdout)

    def test_kuma_fresh_account_uses_explicit_setup_state(self):
        report = self.execute(fresh=True)
        self.assertEqual(report["code"], 0)
        self.assertEqual(report["calls"], ["needSetup", "setup", "login", "getMonitorList"])
        self.assertTrue(json.loads(report["output"][0])["authenticated_monitor_list"])

    def test_kuma_retries_only_readiness_after_async_handler_race(self):
        report = self.execute(fresh=True, lostReady=True)
        self.assertEqual(report["code"], 0)
        self.assertEqual(report["calls"], ["needSetup", "needSetup", "setup", "login", "getMonitorList"])

    def test_existing_kuma_bad_login_never_attempts_setup(self):
        report = self.execute(badLogin=True)
        self.assertEqual(report["code"], 2)
        self.assertEqual(report["calls"], ["needSetup", "login"])

    def test_invalid_setup_state_never_writes_an_account(self):
        report = self.execute(invalidReady=True)
        self.assertEqual(report["code"], 2)
        self.assertEqual(report["calls"], ["needSetup"] * 3)

    def test_kuma_ack_without_monitor_list_event_is_not_readback(self):
        report = self.execute(missingMonitorEvent=True)
        self.assertEqual(report["code"], 2)
        self.assertEqual(report["output"], [])

    def test_dockge_uses_current_stack_list_instead_of_unknown_detail_state(self):
        report = self.execute(service="dockge")
        self.assertEqual(report["code"], 0)
        project = json.loads(report["output"][0])["projects"][0]
        self.assertEqual(project["status"], 3)
        self.assertEqual(project["service_status"], {"fixture": "running"})
        self.assertIn("requestStackList", report["calls"])

    def test_dockge_reports_intentionally_stopped_other_stages_truthfully(self):
        report = self.execute(service="dockge", stopped=True)
        self.assertEqual(report["code"], 0)
        project = json.loads(report["output"][0])["projects"][0]
        self.assertEqual(project["status"], 4)
        self.assertEqual(project["service_status"], {})

    def test_dockge_refuses_missing_or_foreign_project(self):
        for fixture in ({"missingStack": True}, {"foreign": True}):
            with self.subTest(fixture=fixture):
                report = self.execute(service="dockge", **fixture)
                self.assertEqual(report["code"], 2)

    def test_dockge_enrolls_only_after_explicit_first_run_announcement(self):
        fresh = self.execute(service="dockge", fresh=True)
        self.assertEqual(fresh["code"], 0)
        self.assertEqual(fresh["calls"][:3], ["login", "setup", "login"])
        existing = self.execute(service="dockge", badLogin=True)
        self.assertEqual(existing["code"], 2)
        self.assertNotIn("setup", existing["calls"])


if __name__ == "__main__":
    unittest.main()
