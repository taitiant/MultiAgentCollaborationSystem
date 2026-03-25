package macs.core;
import java.util.Map;

public record Event(
        String eventId,
        long timestamp,
        String actorId,
        String taskId,
        String eventType,
        Map<String, Object> payload) {}
