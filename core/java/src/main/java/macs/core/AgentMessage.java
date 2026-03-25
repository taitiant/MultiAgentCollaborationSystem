package macs.core;
import java.util.List;
import java.util.Map;

public record AgentMessage(
        String messageId,
        String taskId,
        String actorId,
        String domain,
        String intent,
        List<String> capabilitiesUsed,
        List<Map<String, Object>> artifacts,
        Map<String, Object> metadata) {}
