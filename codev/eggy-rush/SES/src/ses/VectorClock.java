/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

/**
 *
 * @author Creativ
 */
public class VectorClock {

    private int[] time;

    public VectorClock() {

        this.time = new int[EggyRush.PlayNum];
        for (int i = 0; i < EggyRush.PlayNum; i++) {
            this.time[i] = 0;
        }
    }

    public int getTimeAt(int i) {
        if (i < 0 || i >= time.length) {
            System.out.print("invalid index");
        }

        return this.time[i];

    }

    @Override
    public String toString() {
        return java.util.Arrays.toString(time);
    }

    public TimeStamp TimeStamp() {
        synchronized (this.time) {
            return new TimeStamp(time);
        }
    }

    public synchronized TimeStamp update(TimeStamp ts) { // message order call this
        int[] newtime = ts.value();
        synchronized (this.time) {
            for (int i = 0; i < time.length; i++) {

                newtime[i] = this.time[i] = Math.max(this.time[i], newtime[i]);
            }
        }

        this.notify();

        System.out.println("clock updated: " + this.toString());
        return new TimeStamp(newtime);
    }

    public synchronized TimeStamp tick() { // message transport call this
        synchronized (this.time) {
            this.time[EggyRush.myID]++;
            System.out.println("clock ticks: " + this.toString());
            this.notify();
            return new TimeStamp(this.time);
        }

    }
}
