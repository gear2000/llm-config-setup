// Unit test for the iac-guard classifier (pure function — no Pi runtime needed).
// Zero dependencies; runs on Node 22.6+ via native type-stripping.
//   task test
//   # or: node --experimental-strip-types layers/llm/pi/common/extensions/iac-guard.test.ts
import { classifyCommand } from "./iac-guard.ts";

type Tier = "allow" | "ask" | "gray";
const cases: Array<[string, Tier]> = [
  // ── allow ─────────────────────────────────────────────
  ["terraform plan", "allow"],
  ["terraform plan -destroy", "allow"], // a destroy-plan preview is read-only
  ["tofu validate && terraform fmt", "allow"],
  ["terraform output -json | jq .", "allow"],
  ["aws s3 ls", "allow"],
  ["aws s3 ls s3://bucket", "allow"],
  ["aws sts get-caller-identity", "allow"],
  ["aws ec2 describe-instances", "allow"],
  ["kubectl get pods", "allow"],
  ["kubectl describe pod x", "allow"],
  ['echo "terraform destroy"', "allow"], // not a real call
  ["git commit -m 'terraform destroy'", "allow"], // out of scope, quoted arg
  ["cat main.tf", "allow"],
  ["terraform", "allow"], // bare → help
  ["aws", "allow"],
  ["aws ec2", "allow"],

  // ── ask (deterministic hard-block) ────────────────────
  ["terraform destroy", "ask"],
  ["terraform destroy -auto-approve", "ask"],
  ["tofu destroy", "ask"],
  ["terraform -chdir=infra destroy", "ask"],
  ["cd /tmp && terraform destroy", "ask"],
  ['bash -c "terraform destroy"', "ask"],
  ["terraform state rm aws_instance.x", "ask"],
  ["AWS_PROFILE=p aws iam delete-user --user-name x", "ask"],
  ["sudo kubectl delete pod x", "ask"],
  ["aws ec2 terminate-instances --instance-ids i-abc", "ask"],
  ["aws rds delete-db-instance --db-instance-identifier x", "ask"],
  ["aws iam delete-role --role-name x", "ask"],
  ["aws s3 rm s3://b/x --recursive", "ask"],
  ["aws s3 rb s3://b", "ask"],
  ["aws s3 sync ./x s3://b --delete", "ask"],
  ["aws cloudformation delete-stack --stack-name s", "ask"],
  ["aws kms schedule-key-deletion --key-id k", "ask"],
  ["kubectl delete pod x", "ask"],
  ["kubectl delete --all pods", "ask"],
  ["kubectl drain node-1", "ask"],
  ["kubectl apply --prune -f .", "ask"],
  ["kubectl scale --replicas=0 deploy/x", "ask"],
  ["kubectl replace --force -f x.yaml", "ask"],
  ["echo hi && aws ec2 terminate-instances --instance-ids i-1", "ask"],

  // ── gray (LLM verifier decides) ───────────────────────
  ["terraform apply", "gray"],
  ["terraform apply -auto-approve", "gray"],
  ["terraform refresh", "gray"],
  ["aws ec2 modify-instance-attribute --instance-id i --no-source-dest-check", "gray"],
  ["aws lambda update-function-code --function-name x --zip-file y", "gray"],
  ["aws cloudformation update-stack --stack-name s", "gray"],
  ["aws cloudformation deploy --template-file t", "gray"],
  ["aws ecs update-service --service x --force-new-deployment", "gray"],
  ["aws s3 cp a s3://b", "gray"],
  ["aws s3 sync ./x s3://b", "gray"], // no --delete
  ["kubectl apply -f .", "gray"],
  ["kubectl patch deploy x -p {}", "gray"],
  ["kubectl scale --replicas=3 deploy/x", "gray"],
  ["kubectl create deployment x --image=y", "gray"],

  // ── downtime/disruption ops promoted to ASK (user policy) ──
  ["aws ec2 stop-instances --instance-ids i-1", "ask"],
  ["aws rds stop-db-instance --db-instance-identifier x", "ask"],
  ["aws ec2 reboot-instances --instance-ids i-1", "ask"],
  ["aws rds reboot-db-instance --db-instance-identifier x", "ask"],
  ["kubectl rollout restart deploy/x", "ask"],
  ["kubectl rollout undo deploy/x", "ask"],
  ["kubectl cordon node-1", "ask"],
  ["kubectl rollout status deploy/x", "allow"], // read-only rollout subcommand
  ["aws ec2 start-instances --instance-ids i-1", "gray"], // start stays gray
  ["kubectl uncordon node-1", "gray"], // reverse of cordon stays gray
];

let pass = 0;
const fails: string[] = [];
for (const [cmd, want] of cases) {
  const { tier, reason } = classifyCommand(cmd);
  if (tier === want) pass++;
  else fails.push(`  ✗ [${want} expected, got ${tier}]  ${cmd}  →  ${reason}`);
}
console.log(`iac-guard classifier: ${pass}/${cases.length} passed`);
if (fails.length) {
  console.log("FAILURES:");
  console.log(fails.join("\n"));
  process.exit(1);
}
console.log("ALL PASS ✓");
