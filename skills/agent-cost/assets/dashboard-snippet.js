// agent-cost:begin
function agentCost(d) {
  try {
    const dir = (process.env.AGENT_COST_STATE_DIR || path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'agent-cost')).replace(/^~(?=\/|$)/, os.homedir());
    const bin = JSON.parse(fs.readFileSync(path.join(dir, 'cli.json'), 'utf8')).path;
    const out = require('child_process').execFileSync(bin, ['statusline', '--json'], { input: JSON.stringify(d), timeout: 500, encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] });
    const r = JSON.parse(out);
    return r && typeof r === 'object' ? r : null;
  } catch { return null; }
}
// agent-cost:end
