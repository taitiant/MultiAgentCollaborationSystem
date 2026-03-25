package macs.core;

public interface BaseScheduler {
    String selectAgent(Task task, SystemState state);
}
