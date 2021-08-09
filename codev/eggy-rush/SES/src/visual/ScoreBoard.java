/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package visual;

import java.awt.Dimension;
import javax.swing.JFrame;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import ses.EggyRush;

/**
 *
 * @author Creativ
 */
public class ScoreBoard extends JFrame {

    private JScrollPane scrollPane;
    private JTable table;
    private ScoreReflector model;
    
    public void reference(int[] score){
        this.model.reference(score);
        new Thread(model).start();
    }

    public ScoreBoard() {
        model = new ScoreReflector(EggyRush.PlayNum, 2);
        table = new JTable();
        this.table.setModel(model);
        this.scrollPane = new JScrollPane();
        this.scrollPane.setViewportView(table);
        
        this.add(scrollPane);
        setSize(new Dimension(table.getWidth(), 200));
    }

}
