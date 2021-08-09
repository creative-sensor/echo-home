/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package visual;

import com.sun.org.apache.xalan.internal.xsltc.compiler.util.StringStack;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.swing.table.DefaultTableModel;

/**
 *
 * @author Creativ
 */
public class ScoreReflector extends DefaultTableModel implements Runnable {

    private int[] score;

    public void reference(int[] score) {
        this.score = score;
        for (int i = 0; i < score.length; i++) {
            this.setValueAt(Integer.toString(i), i, 0);
            this.setValueAt(Integer.toString(0), i, 1);
        }
    }

    public ScoreReflector(int rowCount, int columnCount) {
        super(rowCount, columnCount);
        setColumnIdentifiers(new String[] {"Player ID", "Score"});
    }

    @Override
    public void run() {
        while (true) {
            synchronized (this.score) {
                try {
                    this.score.wait();
                } catch (InterruptedException ex) {
                    Logger.getLogger(ScoreReflector.class.getName()).log(Level.SEVERE, null, ex);
                }
            }

            for (int i = 0; i < score.length; i++) {
                this.setValueAt(Integer.toString(score[i]), i, 1);
            }

            fireTableChanged(null);
        }
    }
}
