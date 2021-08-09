/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

/**
 *
 * @author Creativ
 */
public class TimeStamp {

    int[] t;

    public TimeStamp(String str) { // str in format [a,b,c,d,..]
        String liststr = str.substring(1, str.length() - 1);
        String[] tsstr = liststr.split(", ");
        this.t = new int[tsstr.length];
        for (int i = 0; i < tsstr.length; i++) {
            this.t[i] = Integer.parseInt(tsstr[i]);
        }

    }

    public TimeStamp(int[] v) {
        int[] copyvalue = new int[v.length];
        for (int i = 0; i < v.length; i++) {
            copyvalue[i] = v[i];
        }
        this.t = copyvalue;
    }

    public int getElement(int i) {
        return this.t[i];
    }

    public boolean isNotLessThan(TimeStamp ts) {
        for (int i = 0; i < t.length; i++) {
            if (t[i] < ts.getElement(i)) {
                return false;
            }
        }
        return true;
    }

    public boolean isEqualTo(TimeStamp ts) {
        for (int i = 0; i < t.length; i++) {
            if (t[i] != ts.getElement(i)) {
                return false;
            }
        }
        return true;
    }
    public int[] value() {
        return this.t;
    }

    @Override
    public String toString() {
        return "<timestamp>" + java.util.Arrays.toString(t) + "</timestamp>";
    }
    
    public String toLog(){
        return "\t" + java.util.Arrays.toString(t) + System.getProperty("line.separator");
    }
}
