package macs.core;
import java.util.List;

public interface BaseAgent {
    String getId();
    String getRoleName();
    String getDomain();
    List<String> getCapabilities();
    AgentMessage act(Task task, SystemState state);
}
